**English** | [العربية](HOW-IT-WORKS.ar.md)

# How whatsapp-mauve works (a beginner's guide)

This file explains the project without the jargon. For the technical details see [`src/docs/ARCHITECTURE.md`](src/docs/ARCHITECTURE.md), and for a map of the code see [`src/README.md`](src/README.md).

## The idea in one sentence

The project makes a copy of your WhatsApp account on your computer. The copy has all the old messages from your phone's backup, and every new message as soon as it arrives. Nothing is ever erased from it, including deleted and edited messages. You read it on a web page that looks like WhatsApp Web.

## The three parts

```
            Phone                          Computer (Docker)
 ┌──────────────────────┐        ┌──────────────────────────────────────────┐
 │ crypt15 backup        │──(1)──▶│  importer  ──┐                            │
 │  + media folder       │        │              ▼                            │
 └──────────────────────┘        │         archive.db  ◀── (2) sync ◀─────────┼── WhatsApp (linked device)
                                 │         + media/         (sync)           │
                                 │              │                            │
                                 │              ▼                            │
                                 │        (3) viewer ── web page ────────────┼──▶ browser / phone
                                 └──────────────────────────────────────────┘
```

1. **Importer (import):** runs once at the start, and again each time you take a new backup from your phone.
   - It reads the encrypted WhatsApp backup file, and decrypts it with the 64-digit key.
   - It merges the messages into the database.
   - It links the photos and videos to the `media/` folder without copying them twice.
2. **Sync:** a small program that keeps running in the background.
   - It is linked to your account as a "linked device", the same way WhatsApp Web works, so you scan the QR code only once.
   - Any new message, edit, deletion, status, or new profile picture is written to the database the moment it happens.
   - It is also the part that sends the messages when you type them in the app.
3. **Viewer:** a small web server.
   - It reads the database and shows it as a page that looks like WhatsApp Web: search, media, statuses, sending, and more.
   - It is protected by a PIN, and you can open it from any of your devices through Tailscale.

## Where is the data?

Each WhatsApp account has its own folder, `profiles/<name>/`, and that folder holds **everything**:

| File or folder | What is in it                                                                                   |
| -------------- | ----------------------------------------------------------------------------------------------- |
| `wa.conf`      | The account settings: port, access method, GIF keys...                                         |
| `.wa_key`      | The key that decrypts the backup (secret)                                                       |
| `archive.db`   | All the messages, in one SQLite file                                                            |
| `media/`       | All the photos, videos, and audio, with WhatsApp's own folder names. This is the folder you add to Immich |
| `state/`       | The link session with WhatsApp, the secret keys, and the PIN (secret)                           |
| `user/`        | Your things: bookmarks, labels, profile pictures, saved stickers                                |
| `phone/`       | Encrypted phone backups, for the next import                                                    |
| `cache/`       | Thumbnails and things that are created automatically. You can delete them at any time           |

**The golden rule:** if you have a copy of `profiles/<name>/` and a copy of the code, you have everything. So if the server burns down:
1. Download the code.
2. Put the account folder inside `profiles/`.
3. Run `docker compose up -d`.

## Why Docker?

Docker packs the program together with everything it needs (Python, Node, ffmpeg...) into a "box" called an **image**. That way you do not install anything on the machine except Docker itself, and the program works the same way on any machine.

- **Image:** the program only, built from the `src/` folder.
- **Container:** a running copy of the image. Each account has two containers:
  - `<name>-sync`: the sync, which starts by itself when the machine boots.
  - `<name>-viewer`: the viewer.
- **The data never goes into the image.** The container sees the account folder from the outside, which is connected inside it at the path `/data` (this is called a "mount"). So you can delete the image and build it again without losing anything.

## Files at the root of the project

```
whatsapp-mauve/
  README.md, README.ar.md      how to install and use it
  HOW-IT-WORKS.md, .ar.md      this file
  wa                 the control command for everything (wa up, wa status, wa backup...)
  install.sh         the installer: builds the image and creates the first account
  wa.conf.example    the settings template, with every key explained
  compose.yml        generated automatically by `wa regen`, and gathers all the accounts
  profiles/          the data (not uploaded to git)
  src/               all the code
  legacy/            the old way without Docker (only for reference)
```

## Commands you will use

| You want to...               | Type                                                                  |
| ---------------------------- | --------------------------------------------------------------------- |
| Start everything             | `docker compose up -d`                                                |
| Stop everything              | `docker compose down`                                                 |
| See what is running          | `wa status`                                                           |
| See the sync log             | `wa logs <name>`                                                      |
| Create a new account         | `wa new <name>`                                                       |
| Import a new phone backup    | `wa import <name> <path to WhatsApp folder>`                          |
| Link with a QR code          | `wa link <name>`                                                      |
| Set or change the PIN        | `wa pin <name>`                                                       |
| Make a backup                | `wa backup <name> <destination>`                                      |
| Restore a backup on a new machine | `wa restore <destination>/<name>`                                |
| Change the code and apply it | edit in `src/`, then `docker compose build && docker compose up -d`   |

## How changes and deletions are saved

The database is **cumulative**, meaning nothing is ever erased automatically:
- **A message was edited:** the old version is kept, and you can see it in the "edit history".
- **A message was deleted:** it stays there, shown faded.
- **"Delete from archive":** this is the only way to actually remove a message. It removes the message and records that it was removed, so it does not come back from the sync or from a backup.

## Safety in brief

- The viewer only opens from your machine or from your devices on Tailscale. You set this with `WA_ACCESS` in the `wa.conf` file.
- Everything is behind a PIN. The PIN is stored encrypted, and there is a limit on wrong attempts.
- Sending only works when a PIN is set.
- The sync is "hidden": it does not send "read" receipts to anyone.
- The secrets (`.wa_key`, `state/`, `wa.conf`) are never uploaded to git.
