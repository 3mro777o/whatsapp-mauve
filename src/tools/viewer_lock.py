"""PIN lock for the viewer (wa-archive). Files live in WA_STATE, mode 600:
  viewer-pin            scrypt hash of the PIN (no file = no lock)
  viewer-sessions.json  {sha256(session token): expiry}, so restarts keep logins
  api-token             local token for background tools (X-WA-Token header)
wa-archive reads these on every request, so `--set-pin` / `--clear-pin` apply without a restart."""
import getpass, hashlib, hmac, json, os, re, secrets, threading, time
import waconf

PIN_FILE = os.path.join(waconf.STATE, "viewer-pin")
SESSIONS_FILE = os.path.join(waconf.STATE, "viewer-sessions.json")
API_TOKEN_FILE = os.path.join(waconf.STATE, "api-token")
COOKIE = "wa_s"
SESSION_SECS = int(waconf.get("WA_SESSION_DAYS", float) * 86400)
MIN_LEN = 4
GLOBAL = "*"  # throttle key shared by all clients
_mu = threading.Lock()
login_mu = threading.Lock()  # one login attempt at a time: parallel guesses can't slip past the throttle
_fails = {}  # throttle key -> [wrong tries in a row, locked until]


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text)
    os.replace(tmp, path)


def _remove(path):
    try: os.remove(path)
    except FileNotFoundError: pass


# ---- PIN ----
def hash_pin(pin):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def check_pin(pin, stored):
    if not stored or len(pin) > 256: return False
    try:
        alg, n, r, p, salt, want = stored.split("$")
        got = hashlib.scrypt(pin.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(want) // 2)
    except ValueError: return False
    return alg == "scrypt" and hmac.compare_digest(got, bytes.fromhex(want))


def stored_pin():
    """Stored hash, None when there is no PIN file, '' when the file is empty (fails closed)."""
    try:
        with open(PIN_FILE, encoding="utf-8") as f: return f.read().strip()
    except FileNotFoundError: return None


def pin_set(): return os.path.exists(PIN_FILE)


# ---- sessions ----
def _sessions():
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f: d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError): return {}


def _store(d):
    now = time.time()
    _write_private(SESSIONS_FILE, json.dumps({k: v for k, v in d.items() if v > now}))


def _digest(tok): return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def new_session():
    tok = secrets.token_urlsafe(32)
    with _mu:
        d = _sessions(); d[_digest(tok)] = time.time() + SESSION_SECS; _store(d)
    return tok


def session_ok(tok):
    return bool(tok) and _sessions().get(_digest(tok), 0) > time.time()


def drop_session(tok):
    with _mu:
        d = _sessions(); d.pop(_digest(tok), None); _store(d)


def clear_sessions(): _remove(SESSIONS_FILE)


def cookie_token(header):
    m = re.search(r"(?:^|;)\s*wa_s=([A-Za-z0-9_-]+)", header or "")
    return m.group(1) if m else ""


def set_cookie(value, max_age, https):
    return f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}" + ("; Secure" if https else "")


# ---- wrong-PIN throttle: 5 wrong tries in a row -> lock 30s, doubling, capped at 1h ----
def wait_left(key):
    st = _fails.get(key)
    return max(0.0, st[1] - time.time()) if st else 0.0


def record(key, ok):
    with _mu:
        if ok:
            _fails.pop(key, None); return
        st = _fails.setdefault(key, [0, 0.0])
        st[0] += 1
        if st[0] >= 5: st[1] = time.time() + min(3600, 30 * 2 ** min(st[0] - 5, 7))


# ---- local token for tools/avatar-fetcher ----
def api_token():
    try:
        with open(API_TOKEN_FILE, encoding="utf-8") as f: t = f.read().strip()
    except FileNotFoundError: t = ""
    if not t:
        t = secrets.token_urlsafe(32)
        _write_private(API_TOKEN_FILE, t + "\n")
    return t


def cli(argv):
    """`wa-archive --set-pin` / `--clear-pin`. Returns the exit code."""
    if "--clear-pin" in argv:
        _remove(PIN_FILE); clear_sessions()
        print("تم إلغاء رمز القفل. العارض صار بدون قفل.")
        return 0
    a = getpass.getpass(f"رمز جديد للعارض ({MIN_LEN} أحرف على الأقل): ")
    if len(a) < MIN_LEN:
        print(f"لازم {MIN_LEN} أحرف على الأقل. ما تغيّر شي.")
        return 1
    if a != getpass.getpass("أعد كتابة الرمز: "):
        print("الرمزين ما تطابقوا. ما تغيّر شي.")
        return 1
    _write_private(PIN_FILE, hash_pin(a) + "\n")
    clear_sessions()
    print("تم حفظ الرمز. كل الأجهزة لازم تسجّل دخول من جديد.")
    return 0


if __name__ == "__main__":  # self-check: python3 tools/viewer_lock.py (touches no files)
    h = hash_pin("abcd")
    assert check_pin("abcd", h) and not check_pin("abce", h) and not check_pin("", h) and not check_pin("abcd", None)
    assert cookie_token("x=1; wa_s=AbC-_9; y=2") == "AbC-_9" and cookie_token(None) == ""
    for _ in range(5): record("t", False)
    assert 25 < wait_left("t") <= 30
    record("t", True)
    assert wait_left("t") == 0
    print("viewer_lock: ok")
