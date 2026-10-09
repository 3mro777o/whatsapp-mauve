"""Release check (GitHub, cached) and the one-click update request.

The container never touches docker: a request is just a timestamp in <profile>/state/update-request; a HOST systemd
path unit (`wa autoupdate-on`) notices it and runs `wa update`.
"""
import json, os, re, sys, threading, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import waconf

REPO = "3mro777o/whatsapp-mauve"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
EVERY = 12 * 3600       # at most one GitHub call per 12 h (a failed call is retried after 1 h)
CACHE = os.path.join(waconf.HOME, "cache", "update.json")
REQUEST = os.path.join(waconf.STATE, "update-request")
_busy = threading.Lock()


def current():
    try:
        with open(os.path.join(os.path.dirname(waconf.TOOLS), "VERSION"), encoding="utf-8") as f:  # /app/VERSION in the image
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def semver(s):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", s or "")
    return tuple(map(int, m.groups())) if m else None


def enabled():
    return str(waconf.CONF.get("WA_UPDATE_CHECK", "1")).strip() != "0"


def _load():
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, CACHE)


def _fetch():  # -> {"latest","url","notes"}; anything wrong raises
    req = urllib.request.Request(API, headers={"User-Agent": f"whatsapp-mauve/{current()}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read(1 << 20).decode("utf-8"))
    tag = str(d["tag_name"]).lstrip("v")
    url = str(d.get("html_url") or "")
    if not semver(tag) or not url.startswith(f"https://github.com/{REPO}/"):
        raise ValueError("unexpected release")
    return {"latest": tag, "url": url, "notes": str(d.get("body") or "")[:3000]}


def refresh():  # never raises, never blocks a request (callers run it in a thread)
    if not _busy.acquire(False):
        return
    try:
        old = _load()
        try:
            _save({**_fetch(), "checked": time.time()})
        except Exception:
            _save({**old, "checked": time.time() - EVERY + 3600})
    except Exception:
        pass
    finally:
        _busy.release()


def info():
    c = _load()
    on = enabled()
    if on and time.time() - c.get("checked", 0) >= EVERY:
        threading.Thread(target=refresh, daemon=True).start()
    cur, lat = current(), (c.get("latest") if on else None)
    new = bool(lat and semver(lat) and semver(cur) and semver(lat) > semver(cur))
    return {"current": cur, "latest": lat, "available": new, "url": c.get("url") if new else None,
            "notes": c.get("notes") if new else None, "check": on}


def request(now=None):  # -> True when written; False = nothing newer to install
    if not info()["available"]:
        return False
    os.makedirs(os.path.dirname(REQUEST), exist_ok=True)
    tmp = REQUEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"{int(now or time.time())}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, REQUEST)
    return True


if __name__ == "__main__":  # self-check, offline: HOME is a temp dir, GitHub is mocked
    import tempfile
    assert semver("v1.2.3") == (1, 2, 3) and semver("1.10.0") > semver("1.9.9") and semver("x") is None
    t = tempfile.mkdtemp()
    CACHE, REQUEST = os.path.join(t, "cache", "update.json"), os.path.join(t, "state", "update-request")
    cur = current()
    nxt = "%d.%d.%d" % (lambda v: (v[0], v[1], v[2] + 1))(semver(cur))
    calls = []
    def fake(): calls.append(1); return {"latest": nxt, "url": f"https://github.com/{REPO}/releases/tag/v{nxt}", "notes": "n"}
    _fetch = fake
    i = info(); assert not i["available"] and i["latest"] is None            # empty cache: thread started, answer cached later
    for _ in range(50):
        if calls and not _busy.locked(): break
        time.sleep(0.05)
    i = info(); assert i["available"] and i["latest"] == nxt and i["url"] and len(calls) == 1, i
    assert request(5) and open(REQUEST).read() == "5\n"
    _save({"latest": cur, "url": "u", "notes": "", "checked": time.time()}); assert not info()["available"] and not request()
    _save({"latest": nxt, "checked": time.time()}); waconf.CONF["WA_UPDATE_CHECK"] = "0"
    assert not info()["available"] and not info()["check"]                  # disabled: cache ignored, no thread
    waconf.CONF["WA_UPDATE_CHECK"] = "1"
    def boom(): raise OSError("offline")
    _fetch = boom; _save({"latest": cur, "checked": 0}); refresh(); c = _load()
    assert c["latest"] == cur and 0 < time.time() - c["checked"] - (EVERY - 3600) < 5   # failure: silent, retry in 1 h
    print("updater self-check ok")
