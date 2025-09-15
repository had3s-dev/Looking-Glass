# Discord Seedbox Library Bot

A Discord bot you can deploy to Railway that lists authors and their books from your Whatbox seedbox. It supports:

- Commands: `!authors`, `!books <author>`, `!update`, `!help`
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

Extensions default to: `.epub,.mobi,.pdf,.azw3` but can be customized.

## Commands

- `!authors` – List all authors.
- `!books <author>` – List books for the specified author. Partial, case-insensitive author matches are supported.
- `!update` – Force a rescan of the library.
- `!help` – Show command help.

## Configuration

All settings are provided via environment variables. You can create a local `.env` using `.env.example` as a template.

Required:

- `DISCORD_TOKEN` – Your Discord bot token.
- `SFTP_HOST` – Whatbox SFTP host (e.g., `slotname.whatbox.ca`).
- `SFTP_USERNAME` – Your Whatbox username.
- `SFTP_PASSWORD` or `SSH_KEY_PATH` – Use one; password auth or private key.
- `LIBRARY_ROOT_PATH` – The root directory to scan (e.g., `/home/username/books`).

Optional:

- `COMMAND_PREFIX` (default `!`)
- `SFTP_PORT` (default `22`)
- `FILE_EXTENSIONS` (comma-separated, default `.epub,.mobi,.pdf,.azw3`)
- `PAGE_SIZE` (default `20`)
- `CACHE_TTL_SECONDS` (default `900`)

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
3. Railway will detect the `Procfile` and run `worker: python -m bot`.

### Using SSH Key Auth on Railway

If you prefer key-based auth, you have two options:

- Provide a path to a private key within the container via `SSH_KEY_PATH`. You can commit a read-only deploy key in your repo (not recommended for public repos) or mount it as a Railway variable and write it to a temp file at runtime.
- For most setups, `SFTP_PASSWORD` is simpler.

## Notes

- The bot paginates output using interactive buttons. If your guild disables message content intent or buttons, adjust Discord settings accordingly.
- Large libraries: the bot caches results for `CACHE_TTL_SECONDS` to avoid excessive SFTP calls. `!update` bypasses cache and rescans.

## Project Structure

- `bot/__main__.py` – Entry point, command setup, background tasks
- `bot/config.py` – Loads environment variables
- `bot/scanner.py` – SFTP scanner that builds `{ author: [books...] }`
- `bot/cache.py` – Simple TTL cache
- `bot/paginator.py` – Button-based paginator
- `requirements.txt` – Dependencies
- `Procfile` – Railway process definition

## Troubleshooting

- Authentication failures: verify `SFTP_HOST`, `SFTP_USERNAME`, and `SFTP_PASSWORD` or `SSH_KEY_PATH`.
- Empty authors list: confirm `LIBRARY_ROOT_PATH` exists and the process can access it.
- Slow responses: increase `CACHE_TTL_SECONDS` and/or decrease `PAGE_SIZE`.
