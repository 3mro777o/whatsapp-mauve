#!/usr/bin/env bash
# whatsapp-mauve - Docker installer wizard.
#   ./install.sh            install `wa`, build the image, create a profile (guided) in profiles/<name>/
#   ./install.sh --no-instance   only `wa` + image  (then: wa new <name>; cd <repo> && docker compose up -d)
# Re-run any time: every step is idempotent. Old non-docker install (unmaintained): legacy/setup.sh
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
ask()  { local _v; printf '%s [%s]: ' "$2" "$3" >&2; read -r _v || _v=""; printf -v "$1" '%s' "${_v:-$3}"; }
setc() { local v="${3//\\/\\\\}"; v="${v//&/\\&}"; v="${v//|/\\|}"; sed -i "s|^$2=.*|$2=$v|" "$1/wa.conf"; }   # setc DIR KEY VALUE (safe for & | \\)
yn()   { local _a; ask _a "$1" "${2:-n}"; [[ "$_a" =~ ^[Yyن] ]]; }

# ---------- requirements ----------
for t in docker python3; do command -v "$t" >/dev/null || { echo "missing: $t - install it first."; exit 1; }; done
docker compose version >/dev/null 2>&1 || { echo "missing: docker compose plugin."; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker is not running or no permission (sudo usermod -aG docker \$USER)."; exit 1; }
systemctl is-enabled docker >/dev/null 2>&1 || warn "docker is not enabled at boot: sudo systemctl enable --now docker  (sync uses 'restart: unless-stopped')"

# ---------- wa command + image ----------
mkdir -p "$HOME/.local/bin"; ln -sfn "$REPO/wa" "$HOME/.local/bin/wa"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "add ~/.local/bin to PATH: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac
say "wa -> ~/.local/bin/wa"
# image: the published one (ghcr.io/3mro777o/whatsapp-mauve) unless it cannot be pulled (not published/public yet) or WA_IMAGE=whatsapp-mauve:local
# is exported: then the repo builds it locally (developer mode: updates = git pull + rebuild)
IMG="${WA_IMAGE:-ghcr.io/3mro777o/whatsapp-mauve:latest}"; DEV=0
if [ "$IMG" = "whatsapp-mauve:local" ]; then DEV=1
elif docker pull "$IMG"; then say "image $IMG"
else warn "cannot pull $IMG: building locally instead (developer mode)"; DEV=1; fi
if [ "$DEV" = 1 ]; then
  if docker image inspect "whatsapp-mauve:local" >/dev/null 2>&1; then say "image whatsapp-mauve:local exists (wa build to rebuild)"
  else say "building the image (a few minutes)"; nice -n 19 "$REPO/wa" build; fi
fi
[ "${1:-}" = "--no-instance" ] && exit 0
export PATH="$HOME/.local/bin:$PATH"

# ---------- instance wizard ----------
echo
echo "== New WhatsApp account: one self-contained folder profiles/<name> (archive + media + settings) =="
ask NAME "account name (letters/digits)" "me"
if wa list | awk 'NR>1{print $1}' | grep -qxF "$NAME"; then echo "account '$NAME' already exists: wa up/status/import $NAME"; exit 0; fi
ask PORT "viewer port (auto = pick a free one)" "auto"
args=(new "$NAME"); [ "$PORT" = auto ] || args+=(--port "$PORT")
wa "${args[@]}"; DIR="$REPO/profiles/$NAME"
[ "$DEV" = 1 ] && setc "$DIR" WA_IMAGE whatsapp-mauve:local

echo
echo "Viewer access: local = this machine only · tailscale = your tailnet · lan = local network (needs a password)"
ask ACCESS "access" "local"
case "$ACCESS" in local|tailscale|lan) ;; *) warn "unknown '$ACCESS', using local"; ACCESS=local ;; esac
setc "$DIR" WA_ACCESS "$ACCESS"
if [ "$ACCESS" = lan ]; then
  printf '%s: ' "password (user: wa)" >&2; read -rs PW || PW=""; echo >&2
  if [ -n "$PW" ]; then setc "$DIR" WA_PASSWORD "$PW"; else warn "lan needs a password: set WA_PASSWORD in $DIR/wa.conf before 'wa up'"; fi
fi
if [ "$ACCESS" = tailscale ]; then ask TSP "https port for 'tailscale serve' (empty = none)" ""; [ -n "$TSP" ] && setc "$DIR" WA_TS_HTTPS "$TSP"; fi
if yn "start the viewer at boot too? (y/n)" n; then setc "$DIR" WA_VIEWER_AUTOSTART 1; fi

# ---------- backup key ----------
echo
echo "== End-to-end encrypted backup key (64 digits) =="
echo "On the phone: WhatsApp > Settings > Chats > Chat backup > End-to-end encrypted backup > turn on and choose 'Use 64-digit encryption key instead' > copy the key."
echo "If you lost it: turn the encrypted backup off and on again to get a new key, then make a fresh backup (old backups will not open with the new key)."
echo "The key is stored in $DIR/.wa_key (mode 600) and never echoed."
KEY_OK=0
if yn "enter the key now? (y/n)" y; then
  for _ in 1 2 3; do wa key "$NAME" && { KEY_OK=1; break; } || warn "try again"; done
fi

# ---------- phone folder + import ----------
echo
echo "== The phone's WhatsApp folder =="
echo "Copy the phone's WhatsApp folder to this PC (USB/Syncthing): Android/media/com.whatsapp/WhatsApp (or /WhatsApp). It must contain Media/ and Databases/ (with msgstore.db.crypt15)."
ask PHONE "folder path (empty = later)" ""
PHONE="${PHONE/#\~/$HOME}"
if [ -n "$PHONE" ]; then
  if [ "$KEY_OK" = 1 ]; then wa import "$NAME" "$PHONE" || warn "import failed - fix and re-run: wa import $NAME"
  else warn "no key yet: run 'wa key $NAME' then 'wa import $NAME \"$PHONE\"'"; setc "$DIR" WA_PHONE_DIR "$(readlink -f "$PHONE")"; fi
fi

# ---------- start ----------
wa up "$NAME"
if yn "protect the viewer with a PIN? (y/n)" n; then wa pin "$NAME" || true; fi
if yn "link this archive as a WhatsApp device now (QR)? (y/n)" n; then wa link "$NAME"
else echo "link later: wa link $NAME   (WhatsApp > Linked devices > Link a device)"; fi
if command -v notify-send >/dev/null && yn "desktop notification if the link breaks? (y/n)" n; then wa notify-on "$NAME" || true; fi
if yn "one-click updates from the viewer (new release -> 'update now')? (y/n)" n; then wa autoupdate-on "$NAME" || true; fi
echo
say "done: cd $REPO && docker compose up -d  (all profiles) · wa list · wa status $NAME · wa logs $NAME · wa backup $NAME <dest>"
