#!/usr/bin/env bash
# WhatsApp Archive — OLD non-docker install (UNMAINTAINED, kept for reference; the supported way is ../install.sh + docker).
# WhatsApp Archive — install / re-install on this machine (idempotent).
#
#   legacy/setup.sh            deps + commands + systemd user units (sync starts at boot; viewer stays on demand)
#   legacy/setup.sh --no-systemd
#   legacy/setup.sh --uninstall-units
#
# Everything is relative to the repo root (WA_HOME = parent of legacy/). To move to another machine: copy the folder
# (rsync -aH keeps the media hardlinks), run legacy/setup.sh there, done.
set -euo pipefail
WA_HOME="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$WA_HOME"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

conf() {  # conf KEY default  -> value from wa.conf (absolute path for path keys)
  local v; v="$(sed -n "s/^$1=\([^ #]*\).*/\1/p" wa.conf 2>/dev/null | tail -1)"; echo "${v:-$2}"
}
abspath() { case "$1" in /*) echo "$1" ;; *) echo "$WA_HOME/$1" ;; esac; }

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [ "${1:-}" = "--uninstall-units" ]; then
  systemctl --user disable --now wa-sync.service wa-import.path wa-sync-check.service wa-archive.service 2>/dev/null || true
  rm -f "$UNIT_DIR"/wa-archive.service "$UNIT_DIR"/wa-sync.service "$UNIT_DIR"/wa-sync-check.service "$UNIT_DIR"/wa-import.service "$UNIT_DIR"/wa-import.path
  systemctl --user daemon-reload; say "units removed"; exit 0
fi

# ---------- config ----------
[ -f wa.conf ] || { cp wa.conf.example wa.conf; say "created wa.conf from wa.conf.example"; }
STATE="$(abspath "$(conf WA_STATE state)")"; STORE="$(abspath "$(conf WA_STORE archive/store)")"
WEB="$(abspath "$(conf WA_WEB archive/web)")"; BACKUPS="$(abspath "$(conf WA_BACKUPS Databases)")"
AUTH="$(abspath "$(conf WA_AUTH state/auth)")"
mkdir -p "$STATE" "$STORE/Media" "$WEB/user/avatars" "$WEB/thumbs" avatars-inbox/done
chmod 700 "$STATE"

# ---------- system tools ----------
for t in python3 node npm ffmpeg ffprobe pdftoppm notify-send; do
  command -v "$t" >/dev/null || warn "missing: $t (install it with your package manager)"
done
python3 -c 'import PIL' 2>/dev/null || warn "Python Pillow missing (avatars are stored unprocessed): pip install pillow / pacman -S python-pillow"

# ---------- python venv (backup decryption: whatsapp-chat-exporter) ----------
if [ ! -x .venv/bin/python ]; then
  say "creating .venv"; python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import Whatsapp_Chat_Exporter' 2>/dev/null; then
  say "installing whatsapp-chat-exporter into .venv"
  .venv/bin/pip install -q 'whatsapp-chat-exporter[crypt15]' || warn "pip install failed (backup decryption unavailable)"
fi

# ---------- node deps ----------
for d in src/tools/wa-sync src/tools/avatar-fetcher; do
  if [ -f "$d/package.json" ] && [ ! -d "$d/node_modules" ]; then
    say "npm install in $d"; (cd "$d" && npm install --no-audit --no-fund --prefer-offline)
  fi
done

# ---------- session folder (shared by wa-sync and wa-fetch-avatars) ----------
if [ -d src/tools/avatar-fetcher/auth ] && [ ! -L src/tools/avatar-fetcher/auth ] && [ ! -e "$AUTH" ]; then
  mv src/tools/avatar-fetcher/auth "$AUTH"
fi
[ -e src/tools/avatar-fetcher/auth ] || ln -s "$(realpath --relative-to=src/tools/avatar-fetcher "$AUTH")" src/tools/avatar-fetcher/auth

# ---------- relative symlinks inside the web root ----------
if [ ! -e "$WEB/Media" ] || [ -L "$WEB/Media" ]; then
  ln -sfn "$(realpath --relative-to="$WEB" "$STORE/Media")" "$WEB/Media"
fi

# ---------- commands ----------
BIN="$HOME/.local/bin"; mkdir -p "$BIN"
ln -sfn "$WA_HOME/src/tools/wa-archive" "$BIN/wa-archive"
ln -sfn "$WA_HOME/src/tools/wa-search" "$BIN/wa-search"
ln -sfn "$WA_HOME/src/tools/import_avatars.py" "$BIN/wa-avatars"
ln -sfn "$WA_HOME/src/tools/wa-sync/wa-sync" "$BIN/wa-sync"
ln -sfn "$WA_HOME/src/tools/import_msgstore.py" "$BIN/wa-import"
[ -f src/tools/wa-fetch-avatars ] && ln -sfn "$WA_HOME/src/tools/wa-fetch-avatars" "$BIN/wa-fetch-avatars"
say "commands linked into $BIN (wa-archive, wa-sync, wa-import, wa-avatars, wa-search)"

# ---------- archive.db baseline ----------
DB="$(abspath "$(conf WA_DB archive/archive.db)")"
if [ ! -f "$DB" ] && [ -f msgstore.db ]; then
  say "importing msgstore.db into archive.db (baseline)"; python3 src/tools/import_msgstore.py
fi

# ---------- systemd user units ----------
[ "${1:-}" = "--no-systemd" ] && exit 0
if ! command -v systemctl >/dev/null; then warn "no systemd: start 'wa-sync run' yourself"; exit 0; fi
mkdir -p "$UNIT_DIR"
NODE="$(command -v node)"; PY="$(command -v python3)"
for f in legacy/systemd/*.in; do
  out="$UNIT_DIR/$(basename "${f%.in}")"
  sed -e "s|@WA_HOME@|$WA_HOME|g" -e "s|@NODE@|$NODE|g" -e "s|@PYTHON@|$PY|g" \
      -e "s|@WA_BACKUPS@|$BACKUPS|g" -e "s|@PATH@|$(dirname "$NODE"):/usr/local/bin:/usr/bin:/bin|g" "$f" > "$out"
done
systemctl --user daemon-reload
systemctl --user enable wa-sync.service wa-sync-check.service wa-import.path >/dev/null
systemctl --user restart wa-sync.service
systemctl --user start wa-import.path
if [ "$(conf WA_VIEWER_AUTOSTART 0)" = "1" ]; then
  systemctl --user enable wa-archive.service >/dev/null; systemctl --user restart wa-archive.service
  say "viewer: starts at boot (WA_ACCESS=$(conf WA_ACCESS local))"
else
  systemctl --user disable --now wa-archive.service >/dev/null 2>&1 || true
fi
say "units installed: wa-sync (boot, Restart=always), wa-sync-check (login reminder), wa-import.path (new backups)"

# start at boot without logging in
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" 2>/dev/null || pkexec loginctl enable-linger "$USER" 2>/dev/null \
    || warn "could not enable linger: run 'sudo loginctl enable-linger $USER' so wa-sync starts at boot before login"
fi
say "linger: $(loginctl show-user "$USER" -p Linger --value 2>/dev/null)"
say "done. viewer: wa-archive   ·   sync: wa-sync status"
