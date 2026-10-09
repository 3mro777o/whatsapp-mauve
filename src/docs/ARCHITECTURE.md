**English** | [العربية](ARCHITECTURE.ar.md)

# WhatsApp Archive: architecture

> **Path note:** the paths `tools/…`, `docker/…`, and `docs/…` here are relative to the `src/` folder. `wa`, `install.sh`, `compose.yml`, and `profiles/` are at the root. The following commands run from `src/`: `make test`, or `python3 tools/test_import.py`.

A cumulative WhatsApp archive: every backup taken from the phone and every message that arrives live is added to one database, and nothing is ever deleted from it.

```
 Phone ──(crypt15 backup)──► Databases/ ──► wa-import (import_msgstore.py) ──┐
 Phone ──(Media folder)────► Media/ ──hardlink──► archive/store/Media/          │
 WhatsApp ──(linked device)► wa-sync (Node/Baileys) ──JSON lines──► archive_writer.py ─┤
                                                                               ▼
                                                    archive/archive.db  (SQLite WAL)
                                                                               │
                              wa-archive (local server, on demand) ◄────────────┘
                              generates /data/*.json from the database + API + the UI tools/viewer.html
```

## Components

| Component | File | Role |
|---|---|---|
| Settings | `wa.conf` (template: `wa.conf.example`) | All paths and limits. Relative paths are relative to the project folder (`WA_HOME`) |
| Docker | `src/Dockerfile`, `src/docker/`, `compose.yml` (generated), `wa`, `install.sh` | The single image `whatsapp-mauve:local` + the `wa` command for several accounts (`profiles/<name>/`) (see "Docker and multiple accounts") |
| Database | `archive/archive.db`, `tools/schema.sql` | The only source of truth |
| Merge logic | `tools/archive_db.py` | The write rules (nothing is deleted, old versions are kept, deleted messages keep their content) |
| Backup importer | `tools/import_msgstore.py` (`wa-import`) | Decrypts `msgstore*.crypt15` with the key and merges it, and links the `Media/` files to the store |
| Live sync | `tools/wa-sync/sync.mjs` (`wa-sync`) | A "hidden" linked device: it sends no messages and no read receipts, and its state is `unavailable` |
| Writer | `tools/archive_writer.py` | Receives wa-sync events and writes them to the database with the same merge logic, and passes download requests from the viewer along |
| Enrichment | `tools/enrich.py` | Preview images for videos and PDFs, and voice-note waveforms (`archive/web/thumbs/`) |
| Viewer | `tools/wa-archive`, `tools/viewer_data.py`, `tools/viewer.html` | Server on 127.0.0.1:8777, which does not start at boot |
| Alerts | `tools/wa-sync-check` | Desktop notification if the link breaks or the service stops, at every login |

## Database (`archive.db`)

- `chats`: the stable ID is the first part of the JID (the phone number, or `120363…` for groups and channels, or `status`). A `@lid` is converted to the phone number when it is known (`aliases`), so one person has one chat. If a chat ID changes (a LID becomes a number), the change is saved in `chat_renames`, and the viewer converts the old ID automatically.
- `messages`: keyed by `(chat_id, key_id, from_me)`. It holds the current state and extra display fields in `data` (JSON).
- `message_versions`: every older text or caption with its time. `revocations`: the deletion time and who deleted it. `reactions`, `group_members` (current and past members), `contacts` (names from the address book, or the name the person set for themselves).
- `media`: the download state of each file from live sync, with the download keys (mediaKey/directPath) so it can be downloaded later.
- `jobs`: download requests from the viewer. `changes`: the change log that the live update in the UI depends on.

## Merge rules

1. Nothing is ever deleted. Each new backup only adds and updates.
2. A message that became "deleted" (type 15 or REVOKE) keeps its last known content, and the deletion time and who deleted it are recorded. The UI reads it from `"del":{"t","by"}`.
3. When the text changes, the old one is saved in `message_versions` (`"ed":[[ts,text],…]`). An older backup never overwrites a newer text: its text is saved as history only.
4. An older backup that contains the content of a message that was known to be deleted → the content comes back.
5. Media: any file in `Media/` is linked (hardlink) to `archive/store/Media/`, so deleting it from the phone or from the sync folder does not lose it. Live downloads go to `archive/store/Media/live/YYYY-MM/`.

## Live sync (wa-sync)

- A user-level systemd service (`wa-sync.service`) that runs at boot even without a login (linger), with `Restart=always`, low priority (Nice/CPUWeight/IO idle), and a memory limit.
- It uses the linked-device session in `state/auth`. It never deletes it, even if the link breaks.
- Automatic media download up to `WA_MEDIA_MAX_MB` (default 200MB). Anything bigger goes to a waiting list (`/api/media/pending`) with a notification, and it is downloaded when you request it from the UI, as long as it is still on WhatsApp's servers. Media from "chat history" only has its keys saved (`remote`) and is downloaded on request.
- It writes its state to `state/status.json`: `connecting`, `connected`, `disconnected`, `logged_out`, `stopped`, `failed`. A heartbeat is written every 30 seconds.
- `wa-sync-check.service` runs at every graphical login and sends a notification if there is a problem. The service itself also sends an immediate notification when there is a session bus.

Commands: `wa-sync status | logs | restart | stop | pair` (`pair` stops the service, shows the QR code, and then starts it again).

### Edits and view-once messages
- **Edits** now arrive as `secretEncryptedMessage` (AES-GCM without AAD). The key is
  `HKDF-SHA256(messageSecret of the original message, salt=none, info = original id + author jid + editor jid + "Message Edit")`.
  That is why `wa-sync` saves the `messageSecret` of every incoming message to `state/msg-secrets.jsonl` (JSONL, append only, mode 600):
  `{"k": key_id, "s": base64 secret, "a": author jid}`.
- Older messages from the sync take their secrets from the backups: with every import, `import_msgstore.py` adds the missing ones from the `message_secret` table (the author of your messages is `WA_ME` in `wa.conf`, or `me` from `status.json`; if that is unknown, they are skipped),
  and then sends `SIGHUP` to `wa-sync` so it re-reads the file without restarting (`kill -HUP <pid>` does the same thing manually).
- **View-once messages:** Baileys throws the `unavailable` stanza of type `view_once` that reaches linked devices,
  so `wa-sync` catches it from the raw stanza and records a `sys2` message in its place ("📷 view-once message"). The content itself never reaches a linked device at all.

## Remote access (HTTPS via Tailscale)
- `tailscale serve --bg 8777` shows the viewer at `https://<your-device>.<tailnet>.ts.net`, only for devices inside the tailnet.
- ⚠️ Requests that arrive through `tailscale serve` reach the server from `127.0.0.1`, so they are treated as local, and `WA_PASSWORD` does not apply to them.
  The only protection here is the tailnet permissions, so do not run `tailscale funnel` (which is public on the internet).
- To stop: `tailscale serve --https=443 off` (or `tailscale serve reset`).

## Viewer interface (API)

- `/data/chats.json`, `/data/c/<id>.json`, `/data/search.json`, `/data/media.json`, `/data/people.json`: generated from the database with an in-memory cache, and `ETag` and `Cache-Control: no-cache`.
- `GET /api/changes?since=<seq>` returns `{"seq","chats":[…]}`. If it returns `"*"`, the names or IDs changed, so the whole chat list must be reloaded.
- `GET /api/sync/status`, `GET /api/media/pending`, `POST /api/media/fetch/<chatId>/<keyId>`, `POST /api/media/skip/<chatId>/<keyId>`, `GET /api/members/<chatId>`.
- User data (`archive/web/user/`): labels, nicknames and groups, and pictures. It is never deleted.

## Docker and multiple accounts

Project root = **whatsapp-mauve**. One image (`whatsapp-mauve:local`, from `src/Dockerfile`) + the `wa` command (a bash script at the root, installed into `~/.local/bin` by `install.sh`, which resolves the project location with `readlink -f` so it works through the link). Each account = a folder (profile) under `profiles/` that contains everything. The `profiles/` folder is the registry: an account is a folder with a `wa.conf` (nothing in `~/.config`). `profiles/` is excluded from git, and the image is built from `src/` only (`src/.dockerignore`), so the data never enters it.

```
profiles/<name>/wa.conf      all account settings: port, WA_ACCESS, WA_PASSWORD, WA_TS_HTTPS, WA_PHONE_DIR, WA_VIEWER_AUTOSTART
                 compose.yml generated from wa.conf by the wa command (do not edit)
                 archive.db  the database (SQLite WAL)
                 media/      media with WhatsApp names (WhatsApp Images/, WhatsApp Voice Notes/<yyyyww>/, .Statuses/ …)
                 state/      WhatsApp session (auth/), status.json, api-token, the PIN lock, contacts.vcf
                 user/       user data (chat pictures, saved stickers/GIFs, nicknames, labels)
                 cache/      regenerated (web/data, thumbs): not included in backups
                 .wa_key     the 64-digit key (600)
```

- **Running:** the `compose.yml` at the root (`name: whatsapp-mauve`) has an `include:` for each `profiles/<name>/compose.yml` (in long form with `required: false`, so a new copy without accounts does not break). `docker compose up -d` at the root starts all accounts, and `docker compose up -d <name>-sync` or `<name>-viewer` starts a single service. Each `profiles/<name>/compose.yml` is generated by `wa` from `wa.conf`: two services, `<name>-sync` (`restart: unless-stopped`, starts with docker at boot) and `<name>-viewer` (restart depends on `WA_VIEWER_AUTOSTART`), the image `whatsapp-mauve:local` with `build:` (context = project root), so `docker compose up` builds it if it is missing. `network_mode: host` (the same behavior as loopback and `tailscale serve`), ports from `wa.conf`, and `user: uid:gid` for the current user, fixed at generation time. The folder itself is mounted at `/data` (`WA_HOME`) with a relative path (`.:/data`), and the phone folder is mounted read-only at `/phone` if `WA_PHONE_DIR` is set. Limits: `cap_drop: ALL`, `no-new-privileges`, `init`, 1GB memory, and low CPU priority.
- **`wa regen`:** regenerates every account's compose file and the `include` list at the root. It must be run after a restore on another machine (different uid) or after editing `wa.conf` by hand; `wa up`, `wa import`, `wa link`, and `wa pin` do it automatically.
- **Paths:** `waconf.py` resolves everything relative to `WA_HOME`. Keys: `WA_MEDIA_DIR` (the name of the media folder, used as a prefix in the database paths and in the URLs; the default is `Media` for the old install and `media` in a profile), `WA_USER`, and `WA_PHONE_DIR` (sets `WA_PHONE_MEDIA=<>/Media` and `WA_BACKUPS=<>/Databases`). The profile sets `WA_STORE=.`, and `wa-archive` creates the links `cache/web/user` and `cache/web/media`. The old install (`archive/store/Media`, `archive/web/user`) did not change.
- **Import (`wa import`):** (1) on the machine itself: `import_msgstore.py --mirror-only` links (hardlink, or copy if the disk is different) the phone's `Media/` folder into `media/`, and ignores `.Links .Wamo .wamocache .udDH* "WhatsApp Bug Report Attachments" .nomedia`, while keeping `.Statuses`, `WhatsApp AI Media`, and stickers. The reason: containers cannot make hardlinks across different mount points. (2) Inside the container (`docker compose run <name>-sync import`): decrypts the newest `Databases/msgstore*.crypt15` with the key and merges it into the database. Without a key, with a wrong key (not 64 hex digits), or with a damaged file: a clear message and nothing else is broken. The media paths in the database get the `WA_MEDIA_DIR` prefix.
- **Live downloads:** `sync.mjs` saves them into the matching WhatsApp folder: `WhatsApp Images/` (+`Sent/` for sent ones), `WhatsApp Video/`, `WhatsApp Animated Gifs/`, `WhatsApp Voice Notes/<yyyyww>/`, `WhatsApp Audio/`, `WhatsApp Documents/`, `WhatsApp Stickers/`, `.Statuses/`, with names like `IMG-yyyymmdd-WA<id>.ext`. The old paths `media/live/...` still work (the database stores paths relative to the root).
- **Migrating an old install:** `wa migrate-legacy <legacy_dir> <name> [--dry-run]`. It refuses if `wa-sync` (systemd) is running, if the ports (from the old `wa.conf`) are busy, or if the old folder is on a different disk from the project (because it uses `mv`). It moves: `archive/archive.db(+wal/shm)` → `archive.db`, `archive/store/Media/*` → `media/`, `archive/web/user/*` → `user/`, `state/*` (except `handoff*`, logs, and old copies) → `state/`, `.wa_key`, `contacts.vcf` → `state/`, and it builds a new `wa.conf` with the saved settings (`WA_ACCESS WA_PORT WA_SYNC_PORT WA_PASSWORD WA_GIPHY_KEY WA_MEDIA_MAX_MB …`) and relative paths. In the database it replaces `Media/` with `media/` everywhere a path is stored: `media.path`, `messages.data` (`m`, and it recalculates `p` = `thumbs/sha1(m).jpg`, because the thumbnail name is the hash of the path, so it moves with the new name), and `jobs.result`. Permissions: `state` 700 and the secrets 600. `--dry-run` prints every step and the number of rows without changing anything.
- **Desktop notification (optional):** `wa notify-on <name>` installs `wa-notify@<name>.timer` (every 10 minutes; `wa notify` reads `state/status.json` and sends one notification per state).
- **Tailscale:** `WA_TS_HTTPS=<port>` in `wa.conf` → `wa up` runs `tailscale serve --bg --https=<port> http://127.0.0.1:<WA_PORT>`, and `wa down` cancels it.
- **Backup:** `wa backup <name> <dest>` copies the folder (rsync -aH or tar) without `cache/` and `compose.yml`, and the database through `sqlite3 backup` (consistent even while the service is running). Restore: `wa restore <dest>/<name> [--move]` (copies or moves into `profiles/<name>`, then `wa regen`).
- **Tests:** `python3 tools/test_import.py` (imports a fake phone folder and a synthetic msgstore with temporary files, and checks the key errors), and `tools/test_purge.py`.

### Linking `media/` with Immich
An external read-only library: mount `profiles/<name>/media` at `/usr/src/app/external/whatsapp-<name>:ro` in the `immich-server` container, and then *Administration > External Libraries* with the same Import path (Exclusion: `**/.Statuses/**` if you do not want statuses). The names match WhatsApp's, so you can also copy the folder to the phone as it is.

## Moving to another machine or server (old install without docker)

```bash
rsync -aH --info=progress2 WhatsApp/ newhost:~/WhatsApp/   # -H keeps the links between Media/ and the store
ssh newhost 'cd ~/WhatsApp && legacy/setup.sh'                  # venv + npm + commands + systemd services + linger
```

- There is no fixed path in the code. Everything is computed from the project folder, from `wa.conf`, or from `WA_HOME` if it is set in the environment.
- `legacy/setup.sh` writes the service files from the templates in `legacy/systemd/*.in`, with the correct paths for the new machine (node, python, the folder).
- The session (`state/auth`) moves with the folder, and you must not run two copies at the same time with the same session. If you want to link a new device instead of moving: `wa-sync pair`.
- On a server without a graphical interface: the service works normally, and there are no desktop notifications. See `wa-sync status` or `/api/sync/status`.
- To change the port or the interface: `WA_PORT`/`WA_BIND` in `wa.conf`. The server only accepts write requests from the same origin.

## Releases and updates

- **Version:** `src/VERSION` (image: `/app/VERSION` + label `org.opencontainers.image.version`). The viewer exposes it as `v.app_version` in `/api/changes` (also folded into `v.app`, so a new version shows the "new version of the app" toast) and in `/api/version`.
- **Publishing:** `git tag v1.0.1 && git push --tags` runs `.github/workflows/docker.yml` (tag must equal `src/VERSION`): qemu + buildx, context `src/`, linux/amd64 + linux/arm64, pushes `ghcr.io/3mro777o/whatsapp-mauve:{1.0.1,1.0,latest}` with OCI labels (source = this repo, so the package links to it) using `GITHUB_TOKEN` (`packages: write`), then creates the GitHub Release. The package is private after the first push: make it public once in GitHub package settings.
- **Image selection:** `wa` writes `image:` from `WA_IMAGE` (profile `wa.conf`, then environment, default `ghcr.io/3mro777o/whatsapp-mauve:latest`) and adds `build: ../../src` only for the local tag `whatsapp-mauve:local` (developer mode).
- **Update flow:** `tools/updater.py` asks `api.github.com/repos/3mro777o/whatsapp-mauve/releases/latest` at most every 12 h (10 s timeout, no auth, failures silent, retried after 1 h; cache `cache/update.json`; `WA_UPDATE_CHECK=0` disables) and `/api/version` returns `{current, latest, available, url, notes}`. The viewer shows a dismissible banner (dismissed version kept in localStorage). *Update now* = `POST /api/update-request` (PIN gate + same origin, needs a PIN like sending), which only writes a timestamp to `state/update-request`: the container has no docker access. On the host `wa autoupdate-on` installs systemd user units from `src/docker/` (`wa-update@<profile>.path` with `PathExists=` on that file, which starts `wa-update@<profile>.service` = `wa update-request <profile>`: it deletes the file first, so a failure cannot loop, then runs `wa update`). `wa update` = `docker compose pull` + `up -d` (image mode) or `git pull --ff-only` + `build` + `up -d` (developer mode); only services that are running are restarted. The viewer then notices the new app version through the existing toast/reload. `WA_AUTO_UPDATE=1` adds `wa-autoupdate.timer` (nightly `wa update`). Logs: `journalctl --user -u 'wa-update@*' -u wa-autoupdate`. `WA_UPDATE_DRY=1` makes `wa update` only print its commands.

## Notes

- `tools/build_viewer.py` is now old (a static build), and the server uses the database directly. Only its enrichment functions are still used.
- `wa-fetch-avatars` does not work while wa-sync is running, because they use the same session. wa-sync itself fetches the missing pictures once a day.
- Personal statuses are not saved in backups, so they are only recorded from the live sync.

## Viewer access (WA_ACCESS)
| Value | Who can open the viewer |
|---|---|
| `local` | This machine only (the binding is on 127.0.0.1) |
| `tailscale` | This machine plus Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`). Other addresses get 403 |
| `lan` | Also the private networks, and here `WA_PASSWORD` is required |

- `WA_PASSWORD` enables Basic Auth with the user name `wa` for any device other than this machine.
- The API accepts writes only from the same source, meaning `Origin` must match `Host`.
- `WA_VIEWER_AUTOSTART=1` + `legacy/setup.sh` enable `wa-archive.service` at boot.
- With ufw: `sudo ufw allow in on tailscale0 to any port 8777 proto tcp`.

## Viewer PIN lock

- `tools/wa-archive --set-pin` asks for the PIN twice (at least 4 characters), and `--clear-pin` removes it. The PIN is stored with scrypt and a salt in `state/viewer-pin` (mode 600). Without the file, the viewer works as before.
- With a PIN, all paths (the page, `/data`, `/api`, media, PWA) require the `wa_s` cookie (HttpOnly, SameSite=Strict, lasting `WA_SESSION_DAYS`). Without the cookie: `/` redirects to `/login`, and the rest returns 401. `/login`, `POST /api/login`, and `POST /api/logout` are open.
- Sessions are stored as hashes in `state/viewer-sessions.json`, so restarting the server does not log anyone out. Changing or removing the PIN logs out all sessions.
- After 5 wrong attempts in a row: a 30-second lock, which doubles up to one hour. The count is per device (taken from `X-Forwarded-For` behind `tailscale serve`) and also global.
- Background tools (`tools/avatar-fetcher/fetch.mjs`) read `state/api-token` and send it in the `X-WA-Token` header, so they do not need a cookie. wa-sync and the importers write to the database directly and do not use HTTP.
- The service worker (`wa-shell-v4`) does not cache the login page or any 401/302 response.
- Every response includes `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff`, and `Referrer-Policy: no-referrer`, so the viewer cannot be opened inside an iframe. The `/login` page has a strict CSP (`default-src 'none'`, inline script/style, `connect-src 'self'`).
- Media and thumbnails use `private, max-age=86400`, and `/data` uses `private, no-cache` (cached in the browser only, never by any intermediary).
- `GET /api/lock` returns `{"pin": bool}`. The "🔒 Lock now" button in the ⋮ menu appears when a PIN is set, and it runs `POST /api/logout` and returns to `/login`. Any 401 "login required" from the UI redirects to `/login` automatically.
- PDF previews are loaded as a blob (not as an iframe pointing at the server URL), because `DENY` blocks iframes on the same origin.
