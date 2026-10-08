#!/usr/bin/env bash
# whatsapp-mauve - Docker installer wizard / معالج التثبيت (عربي + English).
#   ./install.sh            install `wa`, build the image, create a profile (guided) in profiles/<name>/
#   ./install.sh --no-instance   only `wa` + image  (then: wa new <name>; cd <repo> && docker compose up -d)
# Re-run any time: every step is idempotent. Old non-docker install (unmaintained): legacy/setup.sh
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
both() { printf '%s\n    %s\n' "$1" "$2"; }                      # both "عربي" "English"
ask()  { local _v; printf '%s / %s [%s]: ' "$2" "$3" "$4" >&2; read -r _v || _v=""; printf -v "$1" '%s' "${_v:-$4}"; }
setc() { local v="${3//\\/\\\\}"; v="${v//&/\\&}"; v="${v//|/\\|}"; sed -i "s|^$2=.*|$2=$v|" "$1/wa.conf"; }   # setc DIR KEY VALUE (safe for & | \\)
yn()   { local _a; ask _a "$1" "$2" "${3:-n}"; [[ "$_a" =~ ^[Yyن] ]]; }

# ---------- requirements ----------
for t in docker python3; do command -v "$t" >/dev/null || { both "ناقص: $t — ثبّته أولاً." "missing: $t - install it first."; exit 1; }; done
docker compose version >/dev/null 2>&1 || { both "ناقص: docker compose (plugin)." "missing: docker compose plugin."; exit 1; }
docker info >/dev/null 2>&1 || { both "docker مش شغّال أو ما عندك صلاحية (sudo usermod -aG docker \$USER)." "docker is not running or no permission (sudo usermod -aG docker \$USER)."; exit 1; }
systemctl is-enabled docker >/dev/null 2>&1 || warn "docker is not enabled at boot: sudo systemctl enable --now docker  (sync uses 'restart: unless-stopped')"

# ---------- wa command + image ----------
mkdir -p "$HOME/.local/bin"; ln -sfn "$REPO/wa" "$HOME/.local/bin/wa"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "add ~/.local/bin to PATH: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac
say "wa -> ~/.local/bin/wa"
if docker image inspect "whatsapp-mauve:local" >/dev/null 2>&1; then say "image whatsapp-mauve:local exists (wa build to rebuild)"
else say "building the image (a few minutes) / بناء الصورة"; nice -n 19 "$REPO/wa" build; fi
[ "${1:-}" = "--no-instance" ] && exit 0
export PATH="$HOME/.local/bin:$PATH"

# ---------- instance wizard ----------
echo
both "== حساب واتساب جديد: كل حساب = مجلد مستقل بـ profiles/<الاسم> (أرشيف + وسائط + إعدادات) ==" \
     "== New WhatsApp account: one self-contained folder profiles/<name> (archive + media + settings) =="
ask NAME "اسم الحساب (حروف/أرقام)" "account name (letters/digits)" "me"
if wa list | awk 'NR>1{print $1}' | grep -qxF "$NAME"; then both "الحساب '$NAME' موجود: wa up/status/import $NAME" "account '$NAME' already exists: wa up/status/import $NAME"; exit 0; fi
ask PORT "منفذ العارض (auto = تلقائي)" "viewer port (auto = pick a free one)" "auto"
args=(new "$NAME"); [ "$PORT" = auto ] || args+=(--port "$PORT")
wa "${args[@]}"; DIR="$REPO/profiles/$NAME"

echo
both "الوصول للعارض: local = هالجهاز فقط · tailscale = أجهزتك على Tailscale · lan = الشبكة المحلية (يلزم كلمة سر)" \
     "Viewer access: local = this machine only · tailscale = your tailnet · lan = local network (needs a password)"
ask ACCESS "الوصول" "access" "local"
case "$ACCESS" in local|tailscale|lan) ;; *) warn "unknown '$ACCESS', using local"; ACCESS=local ;; esac
setc "$DIR" WA_ACCESS "$ACCESS"
if [ "$ACCESS" = lan ]; then
  printf '%s / %s: ' "كلمة السر (المستخدم wa)" "password (user: wa)" >&2; read -rs PW || PW=""; echo >&2
  if [ -n "$PW" ]; then setc "$DIR" WA_PASSWORD "$PW"; else warn "lan needs a password: set WA_PASSWORD in $DIR/wa.conf before 'wa up'"; fi
fi
if [ "$ACCESS" = tailscale ]; then ask TSP "منفذ https لـ tailscale serve (فاضي = بدون)" "https port for 'tailscale serve' (empty = none)" ""; [ -n "$TSP" ] && setc "$DIR" WA_TS_HTTPS "$TSP"; fi
if yn "يشتغل العارض مع الإقلاع؟ (y/n)" "start the viewer at boot too? (y/n)" n; then setc "$DIR" WA_VIEWER_AUTOSTART 1; fi

# ---------- backup key ----------
echo
both "== مفتاح النسخ الاحتياطي المشفّر (64 خانة) ==" "== End-to-end encrypted backup key (64 digits) =="
both "على التلفون: واتساب ← الإعدادات ← الدردشات ← النسخ الاحتياطي للدردشات ← نسخ احتياطي مشفّر تمامًا ← فعّله واختر «استخدام مفتاح تشفير من 64 رقمًا» ← انسخ المفتاح." \
     "On the phone: WhatsApp > Settings > Chats > Chat backup > End-to-end encrypted backup > turn on and choose 'Use 64-digit encryption key instead' > copy the key."
both "إذا ضاع المفتاح: أوقف التشفير ثم فعّله من جديد لتحصل على مفتاح جديد، واعمل نسخة احتياطية جديدة (النسخ القديمة ما بتنفتح بالمفتاح الجديد)." \
     "If you lost it: turn the encrypted backup off and on again to get a new key, then make a fresh backup (old backups will not open with the new key)."
both "المفتاح بينحفظ بـ $DIR/.wa_key (صلاحية 600) ولا بينعرض." "The key is stored in $DIR/.wa_key (mode 600) and never echoed."
KEY_OK=0
if yn "تدخل المفتاح هلق؟ (y/n)" "enter the key now? (y/n)" y; then
  for _ in 1 2 3; do wa key "$NAME" && { KEY_OK=1; break; } || warn "try again / أعد المحاولة"; done
fi

# ---------- phone folder + import ----------
echo
both "== مجلد واتساب من التلفون ==" "== The phone's WhatsApp folder =="
both "انسخ مجلد WhatsApp من التلفون للكمبيوتر (USB/Syncthing): Android/media/com.whatsapp/WhatsApp (أو /WhatsApp). لازم يحتوي Media/ و Databases/ (فيه msgstore.db.crypt15)." \
     "Copy the phone's WhatsApp folder to this PC (USB/Syncthing): Android/media/com.whatsapp/WhatsApp (or /WhatsApp). It must contain Media/ and Databases/ (with msgstore.db.crypt15)."
ask PHONE "مسار المجلد (فاضي = لاحقاً)" "folder path (empty = later)" ""
PHONE="${PHONE/#\~/$HOME}"
if [ -n "$PHONE" ]; then
  if [ "$KEY_OK" = 1 ]; then wa import "$NAME" "$PHONE" || warn "import failed - fix and re-run: wa import $NAME"
  else warn "no key yet: run 'wa key $NAME' then 'wa import $NAME \"$PHONE\"'"; setc "$DIR" WA_PHONE_DIR "$(readlink -f "$PHONE")"; fi
fi

# ---------- start ----------
wa up "$NAME"
if yn "تحمي العارض برمز (PIN)؟ (y/n)" "protect the viewer with a PIN? (y/n)" n; then wa pin "$NAME" || true; fi
if yn "ربط الحساب هلق كجهاز مرتبط (QR)؟ (y/n)" "link this archive as a WhatsApp device now (QR)? (y/n)" n; then wa link "$NAME"
else both "للربط لاحقاً: wa link $NAME   (واتساب ← الأجهزة المرتبطة ← ربط جهاز)" "link later: wa link $NAME   (WhatsApp > Linked devices > Link a device)"; fi
if command -v notify-send >/dev/null && yn "إشعار على سطح المكتب إذا انفك الربط؟ (y/n)" "desktop notification if the link breaks? (y/n)" n; then wa notify-on "$NAME" || true; fi
echo
say "done / تم:  cd $REPO && docker compose up -d  (كل الحسابات / all profiles) · wa list · wa status $NAME · wa logs $NAME · wa backup $NAME <dest>"
