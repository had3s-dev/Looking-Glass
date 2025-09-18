# Discord Seedbox Library Bot

A Discord bot you can deploy to Railway that lists your Books/Movies/TV/Music from your Whatbox seedbox and shares expiring links via a lightweight HTTP server.

- Commands: `!browseall` (unified categories UI), `!getbook <author> | <book title>`, `!update`, `!help`
- Optional downloads: `!getbook <author> | <book title>` (small files only; see config)
- SFTP scanning of your library with flexible layout handling
- Automatic background refresh every 30 minutes and manual `!update`
- Pagination with Discord buttons for long lists
- Config via environment variables

## Expected Library Layout

The scanner supports common layouts:

- Nested per author and book directory:
  - `/root/Author Name/Book Title/*.epub`
- Files directly in the author directory:
  - `/root/Author Name/Book Title.epub`
- Flat files under root with pattern `Author - Book.ext` (e.g., `Isaac Asimov - Foundation.epub`)

Extensions default to: `.epub,.mobi,.pdf,.azw3` for books, common video formats for movies/TV, and common audio formats for music, but can be customized.

## Commands

- `!browseall` – Open a single browse UI in Discord with categories (Books/Movies/TV/Music). Selecting an item provides a link page with expiring, signed download links.
- `!update` – Force a rescan of the library.
- `!help` – Show command help.
- `!getbook <author> | <book title>` – Download and upload a book file if downloads are enabled and the file is under the configured upload size.

## Configuration

All settings are provided via environment variables. You can create a local `.env` using `.env.example` as a template.

Required:

- `DISCORD_TOKEN` – Your Discord bot token.
- `SFTP_HOST` – Whatbox SFTP host (e.g., `slotname.whatbox.ca`).
- `SFTP_USERNAME` – Your Whatbox username.
- `SFTP_PASSWORD` or `SSH_KEY_PATH` – Use one; password auth or private key.
- `LIBRARY_ROOT_PATH` – The root directory for books (e.g., `/home/username/books`).

Optional:

- `COMMAND_PREFIX` (default `!`)
- `SFTP_PORT` (default `22`)
- `FILE_EXTENSIONS` (comma-separated, default `.epub,.mobi,.pdf,.azw3`)
- `MOVIES_ROOT_PATH` – Root for movies (e.g., `/media/movies`)
- `MOVIE_EXTENSIONS` – Comma-separated list (default `.mp4,.mkv,.avi,.mov`)
- `TV_ROOT_PATH` – Root for TV shows (e.g., `/media/tv`)
- `TV_EXTENSIONS` – Comma-separated list (default `.mp4,.mkv,.avi,.mov`)
- `MUSIC_ROOT_PATH` – Root for music (e.g., `/media/music`)
- `MUSIC_EXTENSIONS` – Comma-separated list (default `.mp3,.flac,.m4a,.wav`)
- `PAGE_SIZE` (default `20`)
- `CACHE_TTL_SECONDS` (default `900`)
- `ALLOWED_CHANNEL_ID` – If set, restrict commands to a single channel ID.
- `ENABLE_DOWNLOADS` – `true/false` (default `false`). Enables the `!getbook` command.
- `MAX_UPLOAD_BYTES` – Max size for upload to Discord (default `8000000` i.e. ~8MB). Note: Discord server limits may apply depending on Nitro/boost level.

## Local Run

1. Python 3.10+ recommended.
2. Install dependencies:

```
pip install -r requirements.txt
```

3. Create a `.env` file in the project root (see `.env.example`).
4. Run the bot:

```
python -m bot
```

## Deploy on Railway

1. Create a new Railway project and connect your repo, or upload this folder.
2. Set these environment variables in Railway:
   - `DISCORD_TOKEN`
   - `SFTP_HOST`
   - `SFTP_USERNAME`
   - `SFTP_PASSWORD` or configure a variable with your private key file path; see note below.
   - `LIBRARY_ROOT_PATH`
   - Optionally: `SFTP_PORT`, `FILE_EXTENSIONS`, `COMMAND_PREFIX`, `PAGE_SIZE`, `CACHE_TTL_SECONDS`
   - For video player: `ENABLE_VIDEO_PLAYER=true`, `ENABLE_HTTP_LINKS=true`
   - For video transcoding: `FFMPEG_PATH=ffmpeg` (Railway provides this automatically)
3. Railway will detect the `Procfile` and run `worker: python -m bot`.

### Using SSH Key Auth on Railway

If you prefer key-based auth, you have two options:

- Provide a path to a private key within the container via `SSH_KEY_PATH`. You can commit a read-only deploy key in your repo (not recommended for public repos) or mount it as a Railway variable and write it to a temp file at runtime.
- For most setups, `SFTP_PASSWORD` is simpler.

## Notes

- The unified browse UI provides buttons and link pages. If your guild disables message content intent or buttons, adjust Discord settings accordingly.
- Large libraries: the bot caches results for `CACHE_TTL_SECONDS` to avoid excessive SFTP calls. `!update` bypasses cache and rescans.
- Movies/TV/Music scanning is optional; leave their root env vars unset to disable those features.
- Downloads are limited to small files and currently implemented for books only. If you’d like movie/TV/music downloads or link generation (e.g., HTTP links), open an issue or extend the bot accordingly.

### HTTP Link Server

- Set `ENABLE_HTTP_LINKS=true` to enable the internal `aiohttp` server that serves a simple links page and signed download endpoints.
- Configure `HTTP_HOST`, `HTTP_PORT`, `PUBLIC_BASE_URL` (if using a reverse proxy), `LINK_TTL_SECONDS`, and `LINK_SECRET`.

### Video Player

- Set `ENABLE_VIDEO_PLAYER=true` to enable the web-based video player for Movies and TV shows.
- Configure `FFMPEG_PATH` (default: "ffmpeg"), `VIDEO_CACHE_SECONDS` (default: 3600), and `MAX_CONCURRENT_STREAMS` (default: 3).
- The player supports MP4, MKV, WebM, MOV, and AVI files with automatic transcoding for browser compatibility.
- MKV and AVI files are automatically transcoded to MP4 for optimal browser playback.

## Project Structure

- `bot/__main__.py` – Entry point, command setup, background tasks, starts HTTP link server
- `bot/config.py` – Loads environment variables
- `bot/scanner.py` – SFTP scanner that builds `{ author: [books...] }`
- `bot/cache.py` – Simple TTL cache
- `bot/web.py` – Internal `aiohttp` app for links and signed downloads
- `bot/unified_browse.py` – Unified Discord category browser that links to the link pages
- `requirements.txt` – Dependencies
- `Procfile` – Railway process definition

## Troubleshooting

- Authentication failures: verify `SFTP_HOST`, `SFTP_USERNAME`, and `SFTP_PASSWORD` or `SSH_KEY_PATH`.
- Empty authors list: confirm `LIBRARY_ROOT_PATH` exists and the process can access it.
- Slow responses: increase `CACHE_TTL_SECONDS` and/or decrease `PAGE_SIZE`.
