**English** | [العربية](README.ar.md)

# whatsapp-mauve: developer guide

All the code lives here. The project runs in a single Docker image (`whatsapp-mauve:local`). The data lives in `profiles/` at the repo root and is never copied into the image.

## Code map

| File | What it does |
|---|---|
| `tools/wa-archive` | Viewer server and API on `127.0.0.1:8777`, plus the PIN lock. Serves `viewer.html` directly |
| `tools/viewer.html` | Single-file UI (looks like WhatsApp Web). Editing it needs no build, but the container must be updated |
| `tools/viewer_data.py` | Reads `archive.db` for the viewer (chats, messages, media) |
| `tools/viewer_lock.py` | PIN lock and a local token for background tools (`state/api-token`) |
| `tools/wa-sync/sync.mjs` | Live sync: a linked device via Baileys, sending, and read signals |
| `tools/wa-sync/patch-baileys.mjs` | Small patch to Baileys after `npm ci` |
| `tools/archive_writer.py` + `archive_db.py` + `schema.sql` | The only writer to the database, and the merge rules (nothing is deleted, old versions are kept) |
| `tools/import_msgstore.py` | `wa import`: decrypts `msgstore*.crypt15`, merges it, and links `Media/` |
| `tools/sender.py` | Sending, stickers, GIFs (GIPHY and KLIPY), and uploads, behind `wa-archive` |
| `tools/avatars_lib.py`, `import_avatars.py`, `avatar-fetcher/fetch.mjs` | Chat avatars (bulk import, and fetching from WhatsApp) |
| `tools/enrich.py`, `build_viewer.py`, `build_index.py`, `dedupe_library.py`, `build_emoji.py` | Enrichment (preview images, voice-note waveforms), an old static build, and maintenance tools |
| `tools/waconf.py` | Reads `wa.conf` and computes all paths from `WA_HOME` |
| `tools/web-static/`, `tools/fonts/`, `tools/login.html` | Static files for the UI |
| `tools/make_fake_phone.py`, `test_import.py`, `test_purge.py` | Tests (see below) |
| `docker/entrypoint.sh` | Container entry point: `sync \| viewer \| link \| import \| pin \| clearpin \| shell` |
| `docker/wa-notify@.service`, `.timer` | Desktop notification when the link breaks (installed by `wa notify-on`) |
| `Dockerfile`, `.dockerignore` | The image. The build context is `src/`, and the allow-list only admits code |
| `Makefile` | Development targets: `make test` and others |
| `docs/ARCHITECTURE.md` | Architecture and design decisions (Arabic version: `ARCHITECTURE.ar.md`) |

Note: inside the image the code is at `/app/tools` (`WORKDIR /app`), and the fixed path is `WA_HOME=/data`.

## Rebuild

From the repo root:

    docker compose build && docker compose up -d

Or `../wa build` and then `docker compose up -d`. Each account's `compose.yml` builds from `src/` (`context: ../../src`), so any change inside `src/` needs a rebuild. Changing `profiles/` does not need a rebuild.

## Tests

From `src/`:

    make test                      # test_purge.py + test_import.py (temporary files, never touches your data)
    python3 tools/sender.py        # self-check for sending and GIFs
    python3 tools/viewer_lock.py   # self-check for the lock (touches no files)

The tests run on the host (not inside the container) and use temporary folders.

## Config keys

Every key is documented in `../wa.conf.example` (the real per-account copy is `profiles/<name>/wa.conf`). The names and default values are in the code, in `tools/waconf.py` (`DEFAULTS`). Values from the environment (`WA_*`) override the file.

## Notes

- Paths in the code are relative to where the file is: if you move a file, change the number of `dirname` levels at its top (for example in `waconf.py`, `sync.mjs`, `wa-search`).
- `wa` lives at the root (not here) and builds from `src/`. `install.sh` and `compose.yml` are at the root too.
- The old install without Docker is in `../legacy/`, and it is not maintained.
