"""Sunnify Railway backend — drop-in SpotiFLAC replacement.

Endpoints match SpotiFLAC's API shape so MusicaApp works unchanged:
  GET  /home
  GET  /search?q=...&limit=...&type=track
  GET  /metadata?url=...
  GET  /lyrics/<track_id>?track=...&artist=...&format=json
  GET  /stream-audio/<spotify_id>
  GET  /prefetch/<spotify_id>
  POST /download
  GET  /recommendations/<track_id>?limit=...
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import requests as _req
from flask import Flask, Response, abort, jsonify, request, send_file
from flask_cors import CORS

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from spotifydown_api import (  # noqa: E402
    PlaylistClient,
    SpotifyDownAPIError,
    SpotifyEmbedAPI,
    detect_spotify_url_type,
)

# ── Config ─────────────────────────────────────────────────────────────────────

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/sunnify"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Public playlist used only to seed an anonymous Spotify token
_TOKEN_SEED = "37i9dQZF1DXcBWIGoYBM5M"  # Today's Top Hits

# Hardcoded home playlists shown if Spotify featured-playlists call fails
_DEFAULT_HOME: list[dict] = [
    {"id": "37i9dQZF1DXcBWIGoYBM5M", "title": "Today's Top Hits"},
    {"id": "37i9dQZF1DX0XUsuxWHRQd", "title": "RapCaviar"},
    {"id": "37i9dQZF1DX4JAvHpjipBk", "title": "New Music Friday"},
    {"id": "37i9dQZF1DXcF6B6QPhFDv", "title": "Rock Classics"},
    {"id": "37i9dQZF1DX4o1uurTN1HE", "title": "Soft Pop Hits"},
    {"id": "37i9dQZF1DWXRqgorJj26U", "title": "Rock This"},
]

# ── App ────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

_embed_api = SpotifyEmbedAPI()
_playlist_client = PlaylistClient()

# ── Anonymous Spotify token ────────────────────────────────────────────────────

_token_lock = threading.Lock()


def _anon_token() -> str | None:
    with _token_lock:
        try:
            _embed_api._get_access_token(_TOKEN_SEED)
            return _embed_api._cached_token
        except Exception:
            return None


def _spotify(path: str, params: dict | None = None) -> dict | None:
    token = _anon_token()
    if not token:
        return None
    try:
        resp = _req.get(
            f"https://api.spotify.com/v1/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


def _track_obj(t: dict) -> dict:
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    images = t.get("album", {}).get("images", [])
    return {
        "id": t.get("id", ""),
        "name": t.get("name", ""),
        "artists": artists,
        "album_name": t.get("album", {}).get("name", ""),
        "images": images[0]["url"] if images else "",
        "duration_ms": t.get("duration_ms", 0),
        "external_urls": t.get("external_urls", {}).get("spotify", ""),
    }


# ── yt-dlp download ────────────────────────────────────────────────────────────

_dl_locks: dict[str, threading.Lock] = {}
_dl_locks_mu = threading.Lock()


def _lock_for(spotify_id: str) -> threading.Lock:
    with _dl_locks_mu:
        if spotify_id not in _dl_locks:
            _dl_locks[spotify_id] = threading.Lock()
        return _dl_locks[spotify_id]


def _cached_path(spotify_id: str) -> Path | None:
    p = CACHE_DIR / f"{spotify_id}.mp3"
    return p if p.exists() else None


def _normalize(text: str) -> str:
    import unicodedata
    return ''.join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )


def _ytdlp(title: str, artists: str, spotify_id: str) -> Path | None:
    out_tpl = str(CACHE_DIR / f"{spotify_id}.%(ext)s")
    mp3_path = CACHE_DIR / f"{spotify_id}.mp3"

    t, a = title.strip(), artists.strip()
    tn, an = _normalize(t), _normalize(a)

    # Try YouTube Music first, then YouTube, with and without accent normalization
    queries = [
        f"ytmsearch1:{t} {a}",
        f"ytsearch1:{t} {a} audio",
        f"ytmsearch1:{tn} {an}",
        f"ytsearch1:{tn} {an} audio",
        f"ytsearch1:{tn} audio",
    ]

    base_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--output", out_tpl,
        "--extractor-retries", "3",
        "--retries", "3",
    ]

    for query in queries:
        try:
            result = subprocess.run(
                base_cmd + [query],
                capture_output=True,
                timeout=180,
                check=False,
                text=True,
            )
            if mp3_path.exists():
                return mp3_path
            if result.returncode != 0:
                print(f"[yt-dlp] FAILED query={query!r} rc={result.returncode}")
                if result.stderr:
                    print(f"[yt-dlp] stderr: {result.stderr[:500]}")
        except subprocess.TimeoutExpired:
            print(f"[yt-dlp] TIMEOUT query={query!r}")
        except FileNotFoundError:
            print("[yt-dlp] not found — is yt-dlp installed?")
            return None

    return None


def _ensure_downloaded(spotify_id: str) -> Path | None:
    p = _cached_path(spotify_id)
    if p:
        return p
    lock = _lock_for(spotify_id)
    with lock:
        p = _cached_path(spotify_id)
        if p:
            return p
        try:
            track = _embed_api.get_track(spotify_id)
        except SpotifyDownAPIError:
            return None
        return _ytdlp(track.title, track.artists, spotify_id)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.route("/stream-audio/<spotify_id>")
def stream_audio(spotify_id: str):
    p = _ensure_downloaded(spotify_id)
    if p is None:
        abort(502)
    return send_file(p, mimetype="audio/mpeg", conditional=True)


@app.route("/prefetch/<spotify_id>")
def prefetch(spotify_id: str):
    def _bg():
        _ensure_downloaded(spotify_id)

    if not _cached_path(spotify_id):
        threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "ok"})


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    spotify_id = data.get("spotify_id", "").strip()
    if not spotify_id:
        return jsonify({"success": False, "error": "missing spotify_id"}), 400

    p = _cached_path(spotify_id)
    if p:
        return jsonify({"success": True})

    lock = _lock_for(spotify_id)
    with lock:
        p = _cached_path(spotify_id)
        if p:
            return jsonify({"success": True})

        title = data.get("track_name", "").strip()
        artists = data.get("artist_name", "").strip()

        if not title or not artists:
            try:
                track = _embed_api.get_track(spotify_id)
                title, artists = track.title, track.artists
            except SpotifyDownAPIError as e:
                return jsonify({"success": False, "error": str(e)}), 500

        p = _ytdlp(title, artists, spotify_id)
        if p:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": f"Could not find \"{title}\" on YouTube Music or YouTube"}), 502


@app.route("/metadata")
def metadata():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400

    try:
        url_type, item_id = detect_spotify_url_type(url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if url_type == "track":
        try:
            track = _embed_api.get_track(item_id)
        except SpotifyDownAPIError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "track_info": {
                "spotify_id": track.id,
                "name": track.title,
                "artists": track.artists,
                "album_name": track.album or "",
                "images": track.cover_url or "",
                "duration_ms": track.duration_ms or 0,
            }
        })

    # Playlist
    try:
        info = _playlist_client.get_playlist_metadata(item_id)
        tracks = []
        playlist_cover = info.cover_url or ""
        for t in _playlist_client.iter_playlist_tracks(item_id):
            tracks.append({
                "spotify_id": t.id,
                "name": t.title,
                "artists": t.artists,
                "album_name": t.album or "",
                "images": t.cover_url or playlist_cover,
                "duration_ms": t.duration_ms or 0,
                "external_urls": f"https://open.spotify.com/track/{t.id}",
            })
    except SpotifyDownAPIError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "playlist_info": {
            "cover": info.cover_url or "",
            "description": info.description or "",
            "owner": {"display_name": info.owner or "Spotify"},
            "followers": {"total": 0},
            "tracks": {"total": info.track_count or len(tracks)},
        },
        "track_list": tracks,
    })


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 20)), 50)
    if not q:
        return jsonify({"tracks": []})

    result = _spotify("search", {"q": q, "type": "track", "limit": limit})
    if result:
        items = result.get("tracks", {}).get("items", []) or []
        return jsonify({"tracks": [_track_obj(t) for t in items if t]})
    return jsonify({"tracks": []})


@app.route("/home")
def home():
    result = _spotify("browse/featured-playlists", {"limit": 10})
    if result:
        items = result.get("playlists", {}).get("items", []) or []
        playlists = [
            {
                "id": p["id"],
                "title": p["name"],
                "cover": (p.get("images") or [{}])[0].get("url", ""),
            }
            for p in items if p
        ]
    else:
        playlists = list(_DEFAULT_HOME)
        # Try to fetch covers for hardcoded playlists
        for pl in playlists:
            try:
                meta = _embed_api.get_playlist_metadata(pl["id"])
                pl["cover"] = meta.cover_url or ""
            except Exception:
                pl.setdefault("cover", "")

    return jsonify([{"title": "Featured", "playlists": playlists}])


@app.route("/lyrics/<track_id>")
def lyrics(track_id: str):
    track_name = request.args.get("track", "").strip()
    artist_name = request.args.get("artist", "").strip()

    try:
        resp = _req.get(
            "https://lrclib.net/api/search",
            params={"track_name": track_name, "artist_name": artist_name},
            timeout=10,
        )
        if resp.status_code == 200:
            results = resp.json()
            if results:
                synced = results[0].get("syncedLyrics", "")
                if synced:
                    lines = []
                    for line in synced.split("\n"):
                        m = re.match(r"\[(\d+):(\d+\.\d+)\](.*)", line.strip())
                        if m:
                            ms = int((int(m.group(1)) * 60 + float(m.group(2))) * 1000)
                            text = m.group(3).strip()
                            if text:
                                lines.append({"startTimeMs": str(ms), "words": text})
                    return jsonify({"lyrics": {"lines": lines}})
    except Exception:
        pass

    return jsonify({"lyrics": {"lines": []}})


@app.route("/recommendations/<track_id>")
def recommendations(track_id: str):
    limit = int(request.args.get("limit", 20))
    result = _spotify("recommendations", {"seed_tracks": track_id, "limit": limit})
    if result:
        tracks = [_track_obj(t) for t in result.get("tracks", []) if t]
        return jsonify({"tracks": tracks})
    return jsonify({"tracks": []})


@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    return jsonify({
        "name": "Sunnify Backend",
        "version": "1.0.0",
        "endpoints": [
            "GET /home",
            "GET /search",
            "GET /metadata",
            "GET /lyrics/<id>",
            "GET /stream-audio/<id>",
            "GET /prefetch/<id>",
            "POST /download",
            "GET /recommendations/<id>",
        ],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    app.run(host="0.0.0.0", port=port, debug=False)
