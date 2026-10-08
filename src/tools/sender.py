"""Sending from the viewer (used by wa-archive): uploads, ffmpeg conversions, the sticker/GIF library,
archive stickers and GIFs, and the GIPHY proxy. The WhatsApp send itself is tools/wa-sync
(POST /send on 127.0.0.1:WA_SYNC_PORT with X-WA-Token). Stdlib only. `python3 tools/sender.py` = self-check.

  state/outbox/<id>.<ext> (+ <id>.meta with name/mime)   uploads and temp files; older than a day = removed
  archive/web/user/stickers|gifs/<sha16>.webp|mp4         library, meta in stickers.json|gifs.json (fav, last, added)
  GIPHY / KLIPY downloads: https only, media*.giphy.com / i.giphy.com / static[12].klipy.com only, redirects refused, size capped
"""
import concurrent.futures, hashlib, itertools, json, mimetypes, os, re, secrets, shutil, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
import archive_db, viewer_lock, waconf

OUTBOX = os.path.join(waconf.STATE, "outbox")
UPLOAD_MAX = int(float(waconf.CONF["WA_UPLOAD_MAX_MB"] or 100) * 1048576)
SYNC_PORT = int(waconf.CONF["WA_SYNC_PORT"] or 8778)
TYPES = ("text", "image", "video", "document", "ptt", "audio", "sticker", "gif", "forward")
FWD = {"img": "image", "vid": "video", "ptt": "ptt", "doc": "document", "sticker": "sticker"}  # archive kind -> send type
LIB = {"stickers": ("webp", "stickers.json"), "gifs": ("mp4", "gifs.json")}
LABEL = {"img": "📷 صورة", "vid": "🎥 فيديو", "ptt": "🎤 رسالة صوتية", "doc": "📄 مستند", "sticker": "ملصق"}
KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
SHA_RE = re.compile(r"[0-9a-f]{16}")
GIPHY_HOST = re.compile(r"(media[0-9]*|i)\.giphy\.com")
KLIPY_HOST = re.compile(r"static[12]?\.klipy\.com")
_mu = threading.Lock()  # library meta files
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 only, never through a proxy


class Err(Exception):
    """API error: str(e) is the message, e.code the HTTP status."""
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # ponytail: redirects are refused (a hop could leave the allowlist); follow them with a host check per hop if GIPHY ever needs it
    def redirect_request(self, *a, **k): return None


_GET = urllib.request.build_opener(_NoRedirect)


# ---------- small helpers ----------
def _new_id(): return secrets.token_hex(12)


def _clean_ext(e):
    e = re.sub(r"[^a-z0-9]", "", (e or "").lower())[:8]
    return e if e and e != "meta" else "bin"  # "meta" is reserved for upload sidecars


def _upload_ext(name, mime):
    guess = (mimetypes.guess_extension(mime.split(";")[0].strip()) or "").lstrip(".")
    return _clean_ext(os.path.splitext(name)[1].lstrip(".") or guess)


def _outbox_file(ext):
    os.makedirs(OUTBOX, mode=0o700, exist_ok=True)
    return os.path.join(OUTBOX, f"{_new_id()}.{_clean_ext(ext)}")


def _copy(src):
    out = _outbox_file(os.path.splitext(src)[1].lstrip("."))
    shutil.copyfile(src, out)
    return out


def _rm(p):
    if p:
        try: os.remove(p)
        except OSError: pass


def _get_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return default


def _put_json(path, obj):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _q(sql, args=(), one=False):
    """Read-only query on archive.db, on its own connection that is closed after the call."""
    if not os.path.exists(waconf.DB): raise Err(404, "no archive.db")
    db = archive_db.connect(waconf.DB, readonly=True)
    try:
        cur = db.execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()
    finally:
        db.close()


def _sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()[:16]


def _allowed_url(url, host=GIPHY_HOST):
    """SSRF guard for downloads: https, host on the provider's allowlist, no userinfo, default port."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    return u.scheme == "https" and bool(host.fullmatch(u.hostname or "")) and not (u.username or u.password or port)


# ---------- uploads (state/outbox) ----------
def _gc():
    if not os.path.isdir(OUTBOX): return
    cut = time.time() - 86400
    for f in os.listdir(OUTBOX):
        p = os.path.join(OUTBOX, f)
        try:
            if os.path.getmtime(p) < cut: os.remove(p)
        except OSError: pass


def save_upload(rfile, n, name, ctype):
    """Stream the n-byte request body into state/outbox (mode 600). Returns the upload id."""
    _gc()
    name = re.sub(r"[\x00-\x1f]", "", os.path.basename(urllib.parse.unquote(name or "")))[:200].strip() or "file"
    mime = (ctype or "").strip()[:100] or mimetypes.guess_type(name)[0] or "application/octet-stream"
    uid = _new_id()
    os.makedirs(OUTBOX, mode=0o700, exist_ok=True)
    path = os.path.join(OUTBOX, f"{uid}.{_upload_ext(name, mime)}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    got = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while got < n:
                chunk = rfile.read(min(1 << 20, n - got))
                if not chunk: raise Err(400, "upload cut short")
                f.write(chunk)
                got += len(chunk)
        _put_json(os.path.join(OUTBOX, uid + ".meta"), {"name": name, "mime": mime})
    except BaseException:
        _rm(path)
        raise
    return uid


def upload_file(uid):
    """(path, name, mime) of an upload id."""
    if not isinstance(uid, str) or not re.fullmatch(r"[0-9a-f]{24}", uid): raise Err(400, "bad upload id")
    meta = _get_json(os.path.join(OUTBOX, uid + ".meta"), {})
    for f in (os.listdir(OUTBOX) if os.path.isdir(OUTBOX) else []):
        if f.startswith(uid + ".") and not f.endswith((".meta", ".tmp")):
            return os.path.join(OUTBOX, f), meta.get("name") or f, meta.get("mime") or mimetypes.guess_type(f)[0]
    raise Err(404, "upload not found")


# ---------- ffmpeg (nice, 120 s per call) ----------
def _ff(args, timeout=120):
    try:
        r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Err(422, "conversion timed out")
    if r.returncode:
        raise Err(422, "conversion failed: " + r.stderr.decode("utf-8", "replace").strip()[-160:])


def _seconds(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, timeout=20)
        d = float(r.stdout.decode().strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0
    return max(1, round(d)) if d > 0 else 0


def to_ptt(src):  # voice note: mono ogg/opus 48 kHz
    out = _outbox_file("ogg")
    _ff(["-i", src, "-vn", "-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-ar", "48000", "-application", "voip", out])
    return out


def _pil_sticker(src, out):
    """Images (incl. animated webp/gif, which ffmpeg's webp decoder can't read): Pillow -> 512x512 webp. False if not an image."""
    try:
        from PIL import Image, ImageSequence
        im = Image.open(src)
    except Exception: return False
    frames, durs, total = [], [], 0
    for fr in ImageSequence.Iterator(im):
        d = fr.info.get("duration") or im.info.get("duration") or 100
        if frames and total + d > 6000: break  # first 6 s, like the ffmpeg path
        fr = fr.convert("RGBA"); fr.thumbnail((512, 512))
        c = Image.new("RGBA", (512, 512), (0, 0, 0, 0)); c.paste(fr, ((512 - fr.width) // 2, (512 - fr.height) // 2))
        frames.append(c); durs.append(d); total += d
    for q in (70, 45, 25):  # WhatsApp rejects stickers over ~1 MB
        frames[0].save(out, "WEBP", save_all=len(frames) > 1, append_images=frames[1:], duration=durs, loop=0, quality=q, method=4)
        if os.path.getsize(out) < 900_000: break
    return True


def to_sticker(src):  # 512x512 transparent webp, animation kept (first 6 s)
    out = _outbox_file("webp")
    if _pil_sticker(src, out): return out
    # ponytail: one quality pass; WhatsApp rejects stickers over ~1 MB, add a lower-quality retry when that happens
    _ff(["-i", src, "-t", "6", "-vf", "scale=512:512:force_original_aspect_ratio=decrease,format=rgba,"
         "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000,fps=15", "-c:v", "libwebp", "-lossless", "0",
         "-q:v", "70", "-loop", "0", "-an", "-fps_mode", "passthrough", out])
    return out


def to_gif(src):  # GIF playback = H.264 mp4 without audio, even dimensions
    out = _outbox_file("mp4")
    _ff(["-i", src, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "26",
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", out])
    return out


def to_mp4(src):  # camera capture (webm from Chrome/Firefox) -> H.264/AAC mp4, which WhatsApp plays everywhere
    out = _outbox_file("mp4")
    _ff(["-i", src, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "24", "-c:a", "aac", "-b:a", "96k",
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", out], timeout=300)
    return out


# ---------- archive (read-only) ----------
def _archive_cond(kind):
    return "kind = 'sticker'" if kind == "stickers" else "kind = 'vid' and json_extract(data, '$.gif') = 1"


def archive_row(cid, idx):
    """The idx-th message of a chat, in the same order viewer_data.compile_chat uses."""
    try: idx = int(idx)
    except (TypeError, ValueError): raise Err(400, "bad idx")
    if idx < 0: raise Err(400, "bad idx")
    r = _q("select * from messages where chat_id=? order by ts, sort, id limit 1 offset ?", (str(cid or ""), idx), one=True)
    if r is None: raise Err(404, "message not found")
    return r


def archive_media_path(m):
    """Absolute path of a store-relative media path ("Media/...") that really lies inside the store."""
    store = os.path.realpath(waconf.STORE)
    p = os.path.realpath(os.path.join(store, m)) if isinstance(m, str) and m else ""
    if not p.startswith(store + os.sep) or not os.path.isfile(p): raise Err(404, "media file missing")
    return p


def archive_items(kind):
    rows = _q(f"""select chat_id, idx, data from (select chat_id, kind, data, ts, sort, id,
                  row_number() over (partition by chat_id order by ts, sort, id) - 1 as idx from messages)
                  where {_archive_cond(kind)} order by ts desc, sort desc limit 400""")
    out = []
    for r in rows:
        m = json.loads(r["data"] or "{}").get("m")
        try: archive_media_path(m)
        except Err: continue
        out.append({"chat": r["chat_id"], "idx": r["idx"], "url": "/" + urllib.parse.quote(m)})
    return out


def chat_jid(cid):
    r = _q("select jid, merged_into from chats where id=?", (str(cid or ""),), one=True)
    if r is None: raise Err(404, "chat not found")
    if r["merged_into"]: r = _q("select jid, merged_into from chats where id=?", (r["merged_into"],), one=True) or r
    jid = r["jid"]
    if jid.endswith(("@broadcast", "@newsletter", "@temp")): raise Err(400, "cannot send to this chat")
    return jid


def find_msg(r):
    """Archive row of a message ref {key_id, chat?} or {chat, idx}. key_id wins: a late history sync can shift idx, the key id cannot."""
    if not isinstance(r, dict) or not r: raise Err(400, "bad message ref")
    row = None
    if r.get("key_id") is not None:
        k = r["key_id"]
        if not isinstance(k, str) or not KEY_RE.fullmatch(k): raise Err(400, "bad key_id")
        if r.get("chat") is not None:
            row = _q("select * from messages where chat_id=? and key_id=? order by from_me desc limit 1", (str(r["chat"]), k), one=True)
        row = row or _q("select * from messages where key_id=? order by from_me desc limit 1", (k,), one=True)
    elif r.get("chat") is not None and r.get("idx") is not None:
        row = archive_row(r["chat"], r["idx"])
    else:
        raise Err(400, "message ref needs key_id or chat+idx")
    if row is None: raise Err(404, "message not found")
    return row


def reply_of(r):
    """Quote target for wa-sync: {key_id, from_me, sender_jid, text}, taken from archive.db."""
    if not isinstance(r, dict) or not r: return None
    try: row = find_msg(r)
    except Err as e:
        if e.code == 404: raise Err(404, "reply target not found")
        raise
    return {"key_id": row["key_id"], "from_me": bool(row["from_me"]), "sender_jid": row["sender"],
            "text": row["text"] or LABEL.get(row["kind"], "")}


# ---------- GIF / sticker search: GIPHY + KLIPY (keys in wa.conf, never sent to the viewer) ----------
def _fetch_json(url, name):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "whatsapp-mauve/1.0"}), timeout=15) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Err(502, f"{name} HTTP {e.code}")  # the message never carries the URL (KLIPY's key is in its path)
    except (OSError, ValueError):
        raise Err(502, f"{name} unreachable")


def giphy_search(kind, q, offset, limit=50):
    """GET /api/giphy: GIPHY's own JSON (the old route, kept working)."""
    key = (waconf.CONF.get("WA_GIPHY_KEY") or "").strip()
    if not key: raise Err(503, "no key")
    if kind not in LIB: raise Err(400, "bad kind")
    qs = {"api_key": key, "limit": min(50, max(1, int(limit))), "offset": offset, "rating": "g"}  # GIPHY's max is 50 per page
    if q: qs["q"] = q
    return _fetch_json(f"https://api.giphy.com/v1/{kind}/{'search' if q else 'trending'}?{urllib.parse.urlencode(qs)}", "GIPHY")


def klipy_search(kind, q, offset, limit=25):
    """KLIPY's JSON: api/v1/<app key>/<kind>/search|trending; its pages are numbered from 1, so page = offset/limit + 1."""
    key = (waconf.CONF.get("WA_KLIPY_KEY") or "").strip()
    if not key: raise Err(503, "no key")
    if kind not in LIB: raise Err(400, "bad kind")
    qs = {"page": offset // limit + 1, "per_page": limit, "customer_id": "whatsapp-mauve", "content_filter": "high",
          "format_filter": "mp4,webp,jpg" if kind == "gifs" else "webp,gif"}
    if q: qs["q"] = q
    return _fetch_json(f"https://api.klipy.com/api/v1/{urllib.parse.quote(key, safe='')}/{kind}/{'search' if q else 'trending'}?{urllib.parse.urlencode(qs)}", "KLIPY")


def _giphy_item(x, kind):
    """GIPHY result -> common item {id, preview, mp4|webp, w, h}; gifs: still poster + mp4, stickers: animated preview + webp."""
    gid = str(x.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9]{4,64}", gid): return None
    im = x.get("images") or {}
    s = im.get("fixed_height_small") or {}
    still = (im.get("fixed_height_small_still") or {}).get("url") or s.get("url") or ""
    d = {"id": f"giphy:{gid}", "preview": still if kind == "gifs" else (s.get("url") or still),
         "w": int(s.get("width") or 0), "h": int(s.get("height") or 0)}
    d["mp4" if kind == "gifs" else "webp"] = s.get("mp4" if kind == "gifs" else "webp") or ""
    return d if d["preview"] else None


def _klipy_item(x, kind):
    """KLIPY result -> common item; media hosts must pass the same allowlist as downloads, else the item is dropped."""
    kid = str(x.get("id") or "")
    f = x.get("file") or {}
    def pick(*opts):  # first size/format that has a url
        for size, fmt in opts:
            v = (f.get(size) or {}).get(fmt) or {}
            if v.get("url"): return v
        return {}
    media = pick(("md", "mp4"), ("hd", "mp4")) if kind == "gifs" else pick(("md", "webp"), ("hd", "webp"))
    prev = pick(("sm", "jpg"), ("xs", "jpg")) if kind == "gifs" else pick(("sm", "webp"), ("xs", "webp"), ("md", "webp"))
    if not (re.fullmatch(r"[A-Za-z0-9_-]{1,64}", kid) and media and prev): return None
    if not (_allowed_url(media["url"], KLIPY_HOST) and _allowed_url(prev["url"], KLIPY_HOST)): return None
    d = {"id": f"klipy:{kid}", "preview": prev["url"], "w": int(media.get("width") or 0), "h": int(media.get("height") or 0)}
    d["mp4" if kind == "gifs" else "webp"] = media["url"]
    return d


def gif_search(kind, q, offset, limit=25):
    """GET /api/gifsearch: every provider that has a key, queried in parallel, interleaved, deduped by id.
    One provider failing is skipped; {"items", "next" (offset|null), "providers" (who answered)}."""
    if kind not in LIB: raise Err(400, "bad kind")
    limit = min(50, max(8, int(limit)))  # KLIPY's per_page is 8..50
    provs = [(n, fn) for n, fn, key in (("klipy", klipy_search, "WA_KLIPY_KEY"), ("giphy", giphy_search, "WA_GIPHY_KEY"))
             if (waconf.CONF.get(key) or "").strip()]
    if not provs: raise Err(503, "no key")  # the viewer hides the search on this
    with concurrent.futures.ThreadPoolExecutor(len(provs)) as ex:
        futs = [(n, ex.submit(fn, kind, q, offset, limit)) for n, fn in provs]
        got = []
        for n, f in futs:
            try: got.append((n, f.result()))
            except Err: pass
    if not got: raise Err(502, "search unreachable")
    cols, more = [], False
    for n, raw in got:
        if n == "giphy":
            rows, total = raw.get("data") or [], int((raw.get("pagination") or {}).get("total_count") or 0)
            more = more or offset + len(rows) < total
            cols.append([i for i in (_giphy_item(x, kind) for x in rows) if i])
        else:
            page = raw.get("data") or {}
            more = more or bool(page.get("has_next"))
            cols.append([i for i in (_klipy_item(x, kind) for x in page.get("data") or []) if i])
    out, seen = [], set()
    if len(got) == 2 and got[0][0] == "klipy":  # KLIPY first and weighted 2:1 over GIPHY
        k = cols[0]; cols = [k[0::2], k[1::2], cols[1]]
    for row in itertools.zip_longest(*cols):  # interleave the providers; ponytail: dedupe by id only, the same GIF from both providers can repeat
        for it in row:
            if it and it["id"] not in seen: seen.add(it["id"]); out.append(it)
    return {"items": out, "next": offset + limit if more else None, "providers": [n for n, _ in got]}


def remote_fetch(gid, kind, url=None):
    """Download one GIPHY or KLIPY item (kind gifs -> mp4, stickers -> webp) into the outbox. Ids: "giphy:<id>" | "klipy:<id>" (bare = GIPHY).
    GIPHY's URL is built here; KLIPY's media URL comes from the search result and is accepted only on the static[12].klipy.com allowlist."""
    gid = gid if isinstance(gid, str) else ""
    if kind not in LIB: raise Err(400, "bad kind")
    ext = "mp4" if kind == "gifs" else "webp"
    if gid.startswith("klipy:"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", gid[6:]): raise Err(400, "bad klipy id")
        if not (isinstance(url, str) and _allowed_url(url, KLIPY_HOST) and urllib.parse.urlsplit(url).path.lower().endswith("." + ext)):
            raise Err(400, "url not allowed")
    else:
        gid = gid.removeprefix("giphy:")
        if not re.fullmatch(r"[A-Za-z0-9]{4,64}", gid): raise Err(400, "bad giphy id")
        url = f"https://media.giphy.com/media/{gid}/giphy.{ext}"
        if not _allowed_url(url): raise Err(400, "url not allowed")
    out = _outbox_file("bin")
    total = 0
    try:
        with _GET.open(urllib.request.Request(url, headers={"User-Agent": "whatsapp-mauve/1.0"}), timeout=60) as r, open(out, "wb") as f:
            while chunk := r.read(1 << 16):
                total += len(chunk)
                if total > UPLOAD_MAX: raise Err(413, "file too large")
                f.write(chunk)
    except Err:
        _rm(out)
        raise
    except (OSError, ValueError):
        _rm(out)
        raise Err(502, "download failed")
    return out


# ---------- sending ----------
def _source(body, typ):
    """-> (path in outbox, name, mime, raw, temp). raw = still needs converting (upload/GIPHY); temp = we own the file."""
    if body.get("upload"):
        path, name, mime = upload_file(body["upload"])
        return path, name, mime, True, False
    if body.get("giphy"):
        g = body["giphy"] if isinstance(body["giphy"], dict) else {}
        return remote_fetch(g.get("id"), g.get("kind"), g.get("url")), None, None, True, True
    if body.get("lib"):
        kind = "gifs" if typ == "gif" else "stickers"
        return _copy(_lib_src(kind, body["lib"])), None, None, False, True
    if body.get("from"):
        ref = body["from"] if isinstance(body["from"], dict) else {}
        d = json.loads(find_msg(ref)["data"] or "{}")
        src = archive_media_path(d.get("m"))
        return _copy(src), d.get("n") or os.path.basename(src), mimetypes.guess_type(src)[0], typ == "ptt", True  # ptt: re-encode to ogg/opus
    raise Err(400, "no source: upload, giphy, lib or from")


def _post(payload, route="/send", timeout=600):
    """Hand one request to wa-sync (/send, /delete, /react); its answer is {ok, key_id?} or {ok:false, error}."""
    req = urllib.request.Request(f"http://127.0.0.1:{SYNC_PORT}{route}", data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-WA-Token": viewer_lock.api_token()})
    try:
        with _LOCAL.open(req, timeout=timeout) as r: res = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: res = json.loads(e.read() or b"{}")
        except ValueError: res = {}
        raise Err(e.code if e.code in (400, 503) else 502, res.get("error") or f"wa-sync HTTP {e.code}")
    except OSError:
        raise Err(503, "wa-sync is not running")
    if not res.get("ok"): raise Err(502, res.get("error") or "send failed")
    return {"ok": True, "key_id": res.get("key_id")}


def _forward_type(row):
    if row["revoked"] or row["kind"] == "del": raise Err(400, "message was deleted")
    if row["kind"] is None:
        if not (row["text"] or "").strip(): raise Err(400, "nothing to forward")
        return "text"
    if row["kind"] not in FWD: raise Err(400, "cannot forward this message type")
    return "gif" if row["kind"] == "vid" and json.loads(row["data"] or "{}").get("gif") else FWD[row["kind"]]


def send(body):
    """POST /api/send. Returns {ok, key_id}; with dry:true only converts and returns {ok, dry, file, bytes, mime, seconds}."""
    typ, text = body.get("type"), body.get("text") or ""
    if typ not in TYPES: raise Err(400, "bad type")
    if not isinstance(text, str) or len(text) > 65536: raise Err(400, "text too long")
    dry = bool(body.get("dry"))
    jid = chat_jid(body.get("chat"))
    reply = reply_of(body.get("reply"))
    if typ == "forward":  # same content re-sent to another chat: text, or the stored media file; no "Forwarded" label
        # ponytail: Baileys' {forward} flag (label) needs the original proto message, which the archive does not keep
        src = find_msg(body.get("from"))
        typ, text = _forward_type(src), text or src["text"] or ""
        body = {**body, "from": {"chat": src["chat_id"], "key_id": src["key_id"]}}
    if typ == "text":
        if not text.strip(): raise Err(400, "empty text")
        return {"ok": True, "dry": True} if dry else _post({"jid": jid, "type": "text", "text": text, "reply": reply})
    path, name, mime, raw, temp = _source(body, typ)
    tmps = [path] if temp else []
    try:
        if raw and typ == "ptt":
            path = to_ptt(path)
            tmps.append(path)
            mime = "audio/ogg; codecs=opus"
        elif raw and typ == "sticker":
            path = to_sticker(path)
            tmps.append(path)
            mime = "image/webp"
        elif raw and typ == "video" and "webm" in (mime or ""):
            path = to_mp4(path)
            tmps.append(path)
            mime = "video/mp4"
        elif raw and typ == "gif":
            path = to_gif(path)
            tmps.append(path)
            mime = "video/mp4"
        secs = _seconds(path) if typ in ("ptt", "audio") else 0
        if dry:
            return {"ok": True, "dry": True, "file": os.path.basename(path), "bytes": os.path.getsize(path), "mime": mime, "seconds": secs}
        res = _post({"jid": jid, "type": typ, "file": path, "name": name, "mime": mime, "seconds": secs or None,
                     "text": text if typ in ("image", "video", "document", "gif") else None, "reply": reply})
        if body.get("lib"):
            try: lib_set("gifs" if typ == "gif" else "stickers", body["lib"], last=int(time.time()))
            except Err: pass
        return res
    finally:
        if not dry:
            for p in tmps: _rm(p)


# ---------- delete, react (wa-sync /delete, /react) ----------
def _target(body):
    """(archive row, jid) of the message a delete/react points at; not for service rows."""
    row = find_msg(body)
    if row["kind"] in ("sys", "sys2", "call", "del"): raise Err(400, "this message cannot be deleted or reacted to")
    return row, chat_jid(row["chat_id"])


def _me():
    """The linked account: {jid, j, name} from state/me.json (written by wa-sync on connect), else status.json."""
    m = _get_json(os.path.join(waconf.STATE, "me.json"), {})
    jid = m.get("jid") or _get_json(os.path.join(waconf.STATE, "status.json"), {}).get("me")
    if not isinstance(jid, str) or "@" not in jid: return None
    return {"jid": jid, "j": jid.split("@")[0], "name": str(m.get("name") or "")[:200]}


def _wire(row, jid):
    return {"jid": jid, "key_id": row["key_id"], "from_me": bool(row["from_me"]), "participant": None if row["from_me"] else row["sender"]}


def delete_msg(body):
    """POST /api/delete {chat?, idx?, key_id?, scope: me|everyone, dry?} -> {ok, scope, key_id}.
    everyone = revoke for all (own message, or a group admin deleting someone else's); me = deleted from this account's chats only."""
    scope = body.get("scope")
    if scope not in ("me", "everyone"): raise Err(400, "scope must be me or everyone")
    row, jid = _target(body)
    if scope == "everyone":
        if row["revoked"]: raise Err(409, "already deleted for everyone")
        if not row["from_me"]:
            if not jid.endswith("@g.us"): raise Err(403, "cannot delete the other person's message for everyone")
            me = _me()
            a = _q("select admin from group_members where chat_id=? and member=? and active=1", (row["chat_id"], me["jid"]), one=True) if me else None
            if a is not None and not a["admin"]: raise Err(403, "only group admins can delete others' messages for everyone")
    res = {"ok": True, "scope": scope, "key_id": row["key_id"]}
    if body.get("dry"): return {**res, "dry": True}
    _post({**_wire(row, jid), "scope": scope, "ts": int(row["ts"])}, "/delete")
    return res


def react_msg(body):
    """POST /api/react {chat?, idx?, key_id?, emoji ("" = remove), dry?} -> {ok, key_id, emoji}."""
    emoji = body.get("emoji")
    if emoji != "" and not _emoji_ok(emoji): raise Err(400, "bad emoji")
    row, jid = _target(body)
    if row["revoked"]: raise Err(409, "message was deleted")
    res = {"ok": True, "key_id": row["key_id"], "emoji": emoji}
    if body.get("dry"): return {**res, "dry": True}
    _post({**_wire(row, jid), "emoji": emoji}, "/react")
    return res


def chat_perm(cid):
    """GET /api/chatperm/<chat> -> {admin, me_jid}: am I an active admin of this group (from group_members, kept by wa-sync)."""
    c = _q("select id, jid, merged_into from chats where id=?", (str(cid or ""),), one=True)
    if c is None: raise Err(404, "chat not found")
    if c["merged_into"]: c = _q("select id, jid, merged_into from chats where id=?", (c["merged_into"],), one=True) or c
    me = _me()
    a = _q("select admin from group_members where chat_id=? and member=? and active=1", (c["id"], me["jid"]), one=True) if me and c["jid"].endswith("@g.us") else None
    return {"admin": bool(a and a["admin"]), "me_jid": me["jid"] if me else None}


def mark_read(body):
    """POST /api/read {chat} -> {ok, u:0}. unread=0 in archive.db + a changes entry so other open viewers update, then best effort
    markChatAsRead on WhatsApp (app-state, via wa-sync /read). Never sends a read receipt."""
    cid = body.get("chat")
    if not isinstance(cid, str) or not cid: raise Err(400, "chat required")
    c = _q("select id, merged_into from chats where id=?", (cid,), one=True)
    if c is None: raise Err(404, "chat not found")
    cid = c["merged_into"] or c["id"]
    db = archive_db.connect(waconf.DB)
    had = newer = 0
    try:
        archive_db.migrate(db)
        had, rts = db.execute("select coalesce(unread,0), coalesce(read_ts,0) from chats where id=?", (cid,)).fetchone()
        # also when an incoming message is newer than our last read: the phone may still show it unread
        newer = db.execute("select 1 from messages where chat_id=? and from_me=0 and ts>? limit 1", (cid, rts)).fetchone()
        if had or newer:
            now = int(time.time())
            db.execute("update chats set unread=0, read_ts=? where id=?", (now, cid))
            db.execute("insert into changes (chat_id, ts) values (?,?)", (cid, now))
            db.commit()
    finally:
        db.close()
    if had or newer: _read_wa(cid)  # only when something may be unread: no app-state patch per chat open
    return {"ok": True, "u": 0}


def _read_wa(cid):
    """Best effort (never raises): mark the chat read on WhatsApp too so the phone badge clears. wa-sync /read does an
    app-state markChatAsRead with the chat's latest message (chatModify), NOT readMessages: no read receipt reaches the sender."""
    try:
        row = _q("select key_id, from_me, sender, ts from messages where chat_id=? and coalesce(kind,'') not in ('sys','sys2','call','del') "
                 "and not coalesce(revoked,0) order by ts desc, sort desc limit 1", (cid,), one=True)
        if row: _post({**_wire(row, chat_jid(cid)), "ts": int(row["ts"])}, "/read", timeout=15)
    except Exception: pass


# ---------- purge (hard delete from archive.db, tombstoned) ----------
def purge(body):
    """POST /api/purge {items:[{chat,key_id}|{chat,idx}], media?:bool, dry?} -> {ok, removed, files, missing}.
    Rows (+versions, revocations, reactions, media rows) leave archive.db and a tombstone keeps every insert path from bringing them
    back; media=true also deletes the files nobody else references. No message is sent or deleted on WhatsApp."""
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > 1000: raise Err(400, "items must be a list of 1..1000 message refs")
    ids, missing, order = set(), 0, {}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("chat"), str) or not it["chat"]: raise Err(400, "each item needs chat + key_id or idx")
        if it.get("key_id") is not None:
            if not isinstance(it["key_id"], str) or not KEY_RE.fullmatch(it["key_id"]): raise Err(400, "bad key_id")
            rows = _q("select id from messages where chat_id=? and key_id=?", (it["chat"], it["key_id"]))
            ids.update(r["id"] for r in rows)
            missing += not rows
        elif it.get("idx") is not None:
            try: idx = int(it["idx"])
            except (TypeError, ValueError): raise Err(400, "bad idx")
            if it["chat"] not in order:  # same order viewer_data.compile_chat uses, read once per chat before anything is deleted
                order[it["chat"]] = [r["id"] for r in _q("select id from messages where chat_id=? order by ts, sort, id", (it["chat"],))]
            if 0 <= idx < len(order[it["chat"]]): ids.add(order[it["chat"]][idx])
            else: missing += 1
        else:
            raise Err(400, "each item needs chat + key_id or idx")
    if not ids: raise Err(404, "message not found")
    if body.get("dry"): return {"ok": True, "dry": True, "removed": len(ids), "files": 0, "missing": missing}
    db = archive_db.connect(waconf.DB)
    try:
        with open(archive_db.SCHEMA, encoding="utf-8") as f: db.executescript(f.read())  # makes sure `purged` exists (writer may not have restarted yet)
        res = archive_db.purge_messages(db, ids, media=bool(body.get("media")))
    finally:
        db.close()
    _arch_cache.clear()
    return {"ok": True, **res, "missing": missing}


# ---------- emoji (archive/web/user/emoji.json) ----------
EMOJI = os.path.join(waconf.USER, "emoji.json")


def _emoji_ok(e):
    return isinstance(e, str) and 0 < len(e) <= 32 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in e)


def _emoji_state():
    d = _get_json(EMOJI, {})
    return {k: [e for e in (d.get(k) if isinstance(d, dict) else None) or [] if _emoji_ok(e)] for k in ("fav", "recent")}


def emoji_overview():
    """GET /api/emoji -> {fav:[e], recent:[e]}; recent never repeats a favorite (most recent first)."""
    d = _emoji_state()
    return {"fav": d["fav"], "recent": [e for e in d["recent"] if e not in d["fav"]]}


def emoji_set(body, op):
    """POST /api/emoji/used {e} | /api/emoji/fav {e, fav}."""
    e = body.get("e")
    if not _emoji_ok(e): raise Err(400, "bad emoji")
    with _mu:
        d = _emoji_state()
        if op == "used":
            d["recent"] = ([e] + [x for x in d["recent"] if x != e])[:60]
        else:
            d["fav"] = [x for x in d["fav"] if x != e] + ([e] if body.get("fav") else [])
            d["fav"] = d["fav"][:200]
        os.makedirs(waconf.USER, exist_ok=True)
        _put_json(EMOJI, d)
    return {"ok": True}


# ---------- library (archive/web/user/stickers|gifs) ----------
# Item ids: "l:<id>" = saved in the library, "a:<sha16>" = an archive item (sha16 = first 16 hex of the file's sha256, so a
# library file saved from the archive keeps the same hash). The endpoints below also take a bare 16-hex library id.
HASHES = os.path.join(waconf.THUMBS, "media-hash.json")  # {store-relative or user/ path: [mtime, size, sha256 hex]}
POSTERS = os.path.join(waconf.THUMBS, "gif")             # first frame of every GIF: <sha16>.jpg
STK_THUMBS = os.path.join(waconf.THUMBS, "stk")          # small static copy of every sticker: <sha16>.webp
_hmu = threading.Lock()
_hidx = {}
_arch_cache = {}   # kind -> (time, [item]); a few seconds so that paging does not rescan archive.db for every page
_noposter = set()


def _lib_meta(kind): return os.path.join(waconf.USER, kind, LIB[kind][1])


def _lid(x):
    """Library id from "l:<id>" or a bare id."""
    x = x[2:] if isinstance(x, str) and x.startswith("l:") else x
    if not isinstance(x, str) or not SHA_RE.fullmatch(x): raise Err(400, "bad id")
    return x


def lib_file(kind, lid):
    return os.path.join(waconf.USER, kind, f"{_lid(lid)}.{LIB[kind][0]}")


def _lib_src(kind, lid):
    if isinstance(lid, str) and lid.startswith("a:"): return _arch_path(kind, lid)
    p = lib_file(kind, lid)
    if not os.path.isfile(p): raise Err(404, "not in library")
    return p


def _sha_index(files):
    """sha256 hex for [(key, abs path)] (missing files skipped). Cached by key+mtime+size in HASHES; hashing happens outside the lock."""
    global _hidx
    with _hmu:
        if not _hidx: _hidx = _get_json(HASHES, {})
        idx = dict(_hidx)
    out, new = {}, {}
    for key, p in files:
        try: st = os.stat(p)
        except OSError: continue
        c = idx.get(key)
        if c and c[0] == int(st.st_mtime) and c[1] == st.st_size:
            out[key] = c[2]
            continue
        h = hashlib.sha256()
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
        except OSError: continue
        out[key] = h.hexdigest()
        new[key] = [int(st.st_mtime), st.st_size, out[key]]
    if new:
        with _hmu:
            _hidx.update(new)
            os.makedirs(waconf.THUMBS, exist_ok=True)
            try: _put_json(HASHES, _hidx)
            except OSError: pass
    return out


def archive_unique(kind):
    """Archive stickers/GIFs whose file exists, newest message first, one per content sha256: [{id:"a:<sha16>", sha, m, path}]."""
    c = _arch_cache.get(kind)
    if c and time.time() - c[0] < 5: return c[1]
    rows = _q(f"select json_extract(data, '$.m') m from messages where {_archive_cond(kind)} order by ts desc, sort desc, id desc")
    store, files = os.path.realpath(waconf.STORE), []
    for r in rows:
        m = r["m"]
        if not isinstance(m, str) or not m: continue
        p = os.path.realpath(os.path.join(store, m))
        if p.startswith(store + os.sep) and os.path.isfile(p): files.append((m, p))
    # ponytail: a first call hashes every file (~0.4 GB of stickers); wa-archive warms it in a thread at startup
    sha, out, seen = _sha_index(files), [], set()
    for m, p in files:
        h = sha.get(m)
        if h and h not in seen:
            seen.add(h)
            out.append({"id": "a:" + h[:16], "sha": h, "m": m, "path": p})
    _arch_cache[kind] = (time.time(), out)
    return out


def _arch_path(kind, aid):
    h = aid[2:]
    if not SHA_RE.fullmatch(h): raise Err(400, "bad id")
    for it in archive_unique(kind):
        if it["id"] == aid: return it["path"]
    raise Err(404, "not in archive")


def _poster(kind, sha16, src):
    """URL of the cached small still of a library/archive item (made with ffmpeg on first use), or None: a first-frame jpg for GIFs,
    a 160px static webp for stickers (so the picker never decodes 512px animated webps)."""
    g = kind == "gifs"
    d, ext = (POSTERS, "jpg") if g else (STK_THUMBS, "webp")
    f = os.path.join(d, f"{sha16}.{ext}")
    if not os.path.isfile(f) and (kind, sha16) not in _noposter:
        os.makedirs(d, exist_ok=True)
        tmp = f + ".tmp." + ext
        try:
            if g: _ff(["-i", src, "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "6", tmp], timeout=30)
            else:  # Pillow: ffmpeg can't decode animated webp stickers
                from PIL import Image
                im = Image.open(src); im.seek(0); im = im.convert("RGBA"); im.thumbnail((160, 160)); im.save(tmp, "WEBP", quality=60)
            os.replace(tmp, f)
        except (Err, OSError, ImportError, ValueError):
            _rm(tmp)
            _noposter.add((kind, sha16))
    return f"/thumbs/{'gif' if g else 'stk'}/{sha16}.{ext}" if os.path.isfile(f) else None


def _lib_entries(kind):
    """Library as [(id, meta)] whose file exists."""
    meta = _get_json(_lib_meta(kind), {})
    return [(i, m) for i, m in meta.items() if SHA_RE.fullmatch(i) and os.path.isfile(lib_file(kind, i))]


def lib_page(kind, offset, limit):
    """GET /api/{stickers|gifs}?offset=&limit= -> {fav, recent, items, next, total}. fav/recent only at offset 0.
    fav = favorites (most recently used first); recent = the 30 most recently used non-favorites; items = saved but unused library
    items (newest saved first), then archive items (newest first); every list is deduped by content sha256."""
    ext, ents = LIB[kind][0], _lib_entries(kind)
    ids = {i for i, _ in ents}
    def item(i, m): return {"id": "l:" + i, "url": f"/user/{kind}/{i}.{ext}", "fav": bool(m.get("fav")), "last": m.get("last", 0)}
    favs = sorted((e for e in ents if e[1].get("fav")), key=lambda e: (-e[1].get("last", 0), -e[1].get("added", 0)))
    used = sorted((e for e in ents if not e[1].get("fav") and e[1].get("last")), key=lambda e: -e[1]["last"])
    unused = sorted((e for e in ents if not e[1].get("fav") and not e[1].get("last")), key=lambda e: -e[1].get("added", 0))
    recent = used[:30]
    rest = [item(*e) for e in unused + used[30:]]
    rest += [{"id": a["id"], "url": "/" + urllib.parse.quote(a["m"]), "fav": False, "last": 0, "_p": a["path"]}
             for a in archive_unique(kind) if a["sha"][:16] not in ids]
    page = rest[offset:offset + limit]
    out = {"fav": [item(*e) for e in favs] if offset == 0 else [], "recent": [item(*e) for e in recent] if offset == 0 else [],
           "items": page, "next": offset + limit if offset + limit < len(rest) else None, "total": len(rest)}
    jobs = [(it, it.get("_p") or lib_file(kind, it["id"])) for k in ("fav", "recent", "items") for it in out[k]]
    with concurrent.futures.ThreadPoolExecutor(3) as ex:  # missing posters are made 3 at a time; cached after the first page view
        for (it, _), url in zip(jobs, ex.map(lambda j: _poster(kind, j[0]["id"][2:], j[1]), jobs)):
            if url: it["poster"] = url
    for k in ("fav", "recent", "items"):
        for it in out[k]: it.pop("_p", None)
    return out


def lib_overview(kind):
    """Legacy GET /api/{stickers|gifs} without offset/limit: saved items (favorites first, then most recently used) + archive items."""
    ext = LIB[kind][0]
    order = sorted(_lib_entries(kind), key=lambda kv: (not kv[1].get("fav"), -kv[1].get("last", 0), -kv[1].get("added", 0)))
    return {"saved": [{"id": i, "url": f"/user/{kind}/{i}.{ext}", "fav": bool(m.get("fav")), "last": m.get("last", 0)}
                      for i, m in order],
            "archive": archive_items(kind)}


def _adopt(kind, aid):
    """Copy an archive item into the library (id = its sha16) unless it is there already; returns the library id."""
    src, lid = _arch_path(kind, aid), aid[2:]
    with _mu:
        meta = _get_json(_lib_meta(kind), {})
        if lid not in meta or not os.path.isfile(lib_file(kind, lid)):
            os.makedirs(os.path.join(waconf.USER, kind), exist_ok=True)
            tmp = lib_file(kind, lid) + ".tmp"
            shutil.copyfile(src, tmp)
            os.replace(tmp, lib_file(kind, lid))
            meta.setdefault(lid, {"fav": 0, "last": 0, "added": int(time.time())})
            _put_json(_lib_meta(kind), meta)
    return lid


def lib_set(kind, lid, **fields):
    """POST /api/{stickers|gifs}/fav and /used; an "a:<sha16>" archive item is saved into the library first. -> {ok, id:"l:<id>"}."""
    if isinstance(lid, str) and lid.startswith("a:"): lid = _adopt(kind, lid)
    lid = _lid(lid)
    with _mu:
        meta = _get_json(_lib_meta(kind), {})
        if lid not in meta: raise Err(404, "not in library")
        meta[lid].update(fields)
        _put_json(_lib_meta(kind), meta)
    return {"ok": True, "id": "l:" + lid}


def lib_remove(kind, lid):
    """POST /api/{stickers|gifs}/remove {id}: drop one saved item (meta + file)."""
    lid = _lid(lid)
    p = lib_file(kind, lid)
    with _mu:
        meta = _get_json(_lib_meta(kind), {})
        if lid not in meta: raise Err(404, "not in library")
        del meta[lid]
        _put_json(_lib_meta(kind), meta)
        _rm(p)
    return {"ok": True}


def lib_save(kind, body):
    """POST /api/{stickers|gifs}/save with one of: giphy {id,kind} | upload | chat+key | path (archive). Returns {ok, id}."""
    temp, out = [], None
    if body.get("giphy"):
        g = body["giphy"] if isinstance(body["giphy"], dict) else {}
        src, raw = remote_fetch(g.get("id"), g.get("kind") or kind, g.get("url")), True
        temp = [src]
    elif body.get("upload"):
        src, raw = upload_file(body["upload"])[0], True
    elif body.get("chat") and body.get("key"):
        r = _q(f"select data from messages where chat_id=? and key_id=? and ({_archive_cond(kind)}) order by from_me desc limit 1",
               (str(body["chat"]), str(body["key"])), one=True)
        if r is None: raise Err(404, "not in archive")
        src, raw = archive_media_path(json.loads(r["data"] or "{}").get("m")), False
    elif body.get("path"):
        if _q(f"select 1 from messages where ({_archive_cond(kind)}) and json_extract(data, '$.m') = ? limit 1",
              (str(body["path"]),), one=True) is None:
            raise Err(404, "not in archive")
        src, raw = archive_media_path(body["path"]), False
    else:
        raise Err(400, "save needs giphy, upload, chat+key or path")
    try:
        out = ((to_sticker if kind == "stickers" else to_gif)(src)) if raw else _copy(src)
        lid = _sha16(out)
        with _mu:
            meta = _get_json(_lib_meta(kind), {})
            if lid not in meta:
                os.makedirs(os.path.join(waconf.USER, kind), exist_ok=True)
                shutil.move(out, lib_file(kind, lid))
                meta[lid] = {"fav": 0, "last": 0, "added": int(time.time())}
                _put_json(_lib_meta(kind), meta)
        return {"ok": True, "id": lid}
    finally:
        for p in temp + [out]: _rm(p)


if __name__ == "__main__":  # self-check: pure helpers only (no network, no ffmpeg, no files)
    assert _allowed_url("https://media.giphy.com/media/abc123/giphy.mp4") and _allowed_url("https://media3.giphy.com/x")
    assert _allowed_url("https://i.giphy.com/x.webp")
    for bad in ("http://media.giphy.com/x", "https://evil.com/x", "https://media.giphy.com.evil.net/x",
                "https://u@evil.com@media.giphy.com/x", "https://media.giphy.com:8443/x", "file:///etc/passwd"):
        assert not _allowed_url(bad), bad
    assert _allowed_url("https://static.klipy.com/ii/a/b/x.mp4", KLIPY_HOST) and _allowed_url("https://static2.klipy.com/x.webp", KLIPY_HOST)
    for bad in ("https://static3.klipy.com/x", "https://media.giphy.com/x", "https://static.klipy.com.evil.net/x", "http://static.klipy.com/x"):
        assert not _allowed_url(bad, KLIPY_HOST), bad
    k = _klipy_item({"id": 7, "file": {"md": {"mp4": {"url": "https://static.klipy.com/a/x.mp4", "width": 3, "height": 4}},
                                       "sm": {"jpg": {"url": "https://static.klipy.com/a/x.jpg"}}}}, "gifs")
    assert k == {"id": "klipy:7", "preview": "https://static.klipy.com/a/x.jpg", "w": 3, "h": 4, "mp4": "https://static.klipy.com/a/x.mp4"}, k
    assert _klipy_item({"id": 8, "file": {"md": {"webp": {"url": "https://evil.com/x.webp"}}, "sm": {"webp": {"url": "https://static.klipy.com/x.webp"}}}}, "stickers") is None
    assert _upload_ext("Voice.OGG", "") == "ogg" and _upload_ext("", "image/png") == "png" and _upload_ext("x.meta", "") == "bin"
    assert _clean_ext("../../etc") == "etc" and _clean_ext("") == "bin"
    print("sender: ok")
