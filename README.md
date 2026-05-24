# Sunnify Backend

A self-hosted music streaming backend deployable on Railway. Fetches Spotify metadata without authentication and streams audio via yt-dlp.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/stream-audio/<spotify_id>` | Stream track audio as MP3 |
| GET | `/prefetch/<spotify_id>` | Pre-download a track in the background |
| POST | `/download` | Download a track server-side |
| GET | `/metadata?url=<spotify_url>` | Get track or playlist metadata |
| GET | `/search?q=<query>&limit=20` | Search tracks |
| GET | `/lyrics/<spotify_id>?track=&artist=` | Get synced lyrics |
| GET | `/home` | Featured playlists |
| GET | `/recommendations/<spotify_id>` | Similar tracks |
| GET | `/health` | Health check |

## Deploy on Railway

1. Push this repo to GitHub
2. Create a new Railway project → **Deploy from GitHub repo**
3. Railway auto-detects the Dockerfile and builds it
4. Set the `PORT` variable if needed (Railway sets it automatically)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8001` | Server port |
| `CACHE_DIR` | `/tmp/sunnify` | Directory for cached audio files |

## How it works

- **Metadata** — fetched from Spotify's embed page, no API key required
- **Audio** — downloaded from YouTube Music via yt-dlp and cached as MP3
- **Lyrics** — fetched from [lrclib.net](https://lrclib.net) (free, no auth)
- **Search / Recommendations** — uses an anonymous token extracted from Spotify's embed page

## Requirements

Built into the Docker image:
- Python 3.12
- ffmpeg
- yt-dlp
