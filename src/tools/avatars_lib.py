"""Shared helpers for chat avatars (used by tools/wa-archive and tools/import_avatars.py).

Avatars live in archive/web/user/avatars/<j>.<ext> (never wiped by builds), where <j> is the
chat's "j" in chats.json (owner phone number, or the group id). The HTTP API still speaks chat ids.
"""
import io, json, os, re, threading

import sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import waconf

ROOT = waconf.HOME
WEB = waconf.WEB
USER = waconf.USER
AVATARS = os.path.join(USER, "avatars")
MAX_AVATAR = 15 * 1024 * 1024
SIZE = 256

try:
    from PIL import Image, ImageOps
except Exception:  # Pillow missing -> store images as-is
    Image = None


def sniff(data):
    """Return file extension from magic bytes, or None if not a supported image."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def valid_id(chat_id):
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(chat_id)))


# ---- chat id <-> owner number ("j" in chats.json); avatar files are named <j>.<ext> ----
CHATS = os.path.join(WEB, "data", "chats.json")
_map = {"mtime": None, "id2j": {}, "j2ids": {}}
_map_lock = threading.Lock()


def _safe_key(j):
    return bool(j) and bool(re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", j))


_provider = None  # callable -> {chatId: j}; set by the server (dynamic data from archive.db)


def set_provider(fn):
    global _provider
    _provider = fn


def chat_maps():
    """(id2j, j2ids) from the live provider / archive.db, else from the static chats.json."""
    fn = _provider
    if fn is None and os.path.exists(waconf.DB):
        import viewer_data
        vd = viewer_data.ViewerData()
        fn = vd.chat_ids
        set_provider(fn)
    if fn is not None:
        id2j, j2ids = {}, {}
        for cid, j in fn().items():
            if _safe_key(j):
                id2j[cid] = j
                j2ids.setdefault(j, []).append(cid)
        return id2j, j2ids
    try:
        mt = os.path.getmtime(CHATS)
    except OSError:
        return {}, {}
    with _map_lock:
        if _map["mtime"] != mt:
            try:
                with open(CHATS, encoding="utf-8") as f:
                    chats = json.load(f)
            except (OSError, ValueError):
                return _map["id2j"], _map["j2ids"]  # mid-rebuild: keep the previous mapping
            id2j, j2ids = {}, {}
            for c in chats:
                j = str(c.get("j") or "")
                if _safe_key(j):
                    id2j[str(c["id"])] = j
                    j2ids.setdefault(j, []).append(str(c["id"]))
            _map.update(mtime=mt, id2j=id2j, j2ids=j2ids)
        return _map["id2j"], _map["j2ids"]


def key_for(chat_id):
    """Avatar file key (owner number / group id) for a chat id, or None if unknown."""
    return chat_maps()[0].get(str(chat_id))


def _remove_key(key):
    removed = False
    if key and os.path.isdir(AVATARS):
        for f in os.listdir(AVATARS):
            if os.path.splitext(f)[0] == key and not f.startswith(".") and os.path.isfile(os.path.join(AVATARS, f)):
                os.remove(os.path.join(AVATARS, f))
                removed = True
    return removed


HISTORY = os.path.join(AVATARS, "history")  # history/<key>/<unix>.<ext>: unix = when that picture became current (its mtime)
_HIST_FILE = re.compile(r"(\d{1,12})(?:-\d+)?\.(jpg|png|webp|gif)")


def current_file(key):
    """File name of the current avatar of a key, or None."""
    if key and os.path.isdir(AVATARS):
        for f in os.listdir(AVATARS):
            if os.path.splitext(f)[0] == key and not f.startswith(".") and os.path.isfile(os.path.join(AVATARS, f)):
                return f
    return None


def archive_current(key):
    """Move the current avatar into history/<key>/ (never overwrites). True if there was one."""
    f = current_file(key) if _safe_key(key) else None
    if not f:
        return False
    src = os.path.join(AVATARS, f)
    ts = int(os.path.getmtime(src))
    d = os.path.join(HISTORY, key)
    os.makedirs(d, exist_ok=True)
    ext = os.path.splitext(f)[1]
    dst, n = os.path.join(d, f"{ts}{ext}"), 0
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(d, f"{ts}-{n}{ext}")
    os.replace(src, dst)
    return True


def history(chat_id):
    """[{url, ts}] newest first for a chat id (the current avatar is not included)."""
    key = key_for(chat_id)
    d = os.path.join(HISTORY, key) if key else None
    out = []
    if d and os.path.isdir(d):
        for f in os.listdir(d):
            m = _HIST_FILE.fullmatch(f)
            if m:
                out.append({"url": f"/user/avatars/history/{key}/{f}", "ts": int(m.group(1))})
    return sorted(out, key=lambda o: (-o["ts"], o["url"]))


def remove_avatar(chat_id):
    return _remove_key(key_for(chat_id))


def avatar_url(fname):
    mt = int(os.path.getmtime(os.path.join(AVATARS, fname)))
    return f"user/avatars/{fname}?v={mt}"


def list_avatars():
    """{chatId: url} for every avatar file whose <j> maps to one or more chats."""
    out = {}
    j2ids = chat_maps()[1]
    if os.path.isdir(AVATARS):
        for f in sorted(os.listdir(AVATARS)):
            key, ext = os.path.splitext(f)
            if ext and not f.startswith("."):
                for cid in j2ids.get(key, ()):
                    out[cid] = avatar_url(f)
    return out


def save_avatar_key(key, data):
    """Store avatar bytes directly under a number/group key (used by wa-sync)."""
    if not _safe_key(key):
        raise ValueError("invalid key")
    return _store(key, data)


def save_avatar(chat_id, data):
    """Validate + store avatar bytes for chat_id. Returns the URL. Raises ValueError on bad input."""
    if not valid_id(chat_id):
        raise ValueError("invalid chat id")
    key = key_for(chat_id)
    if not key:
        raise ValueError("unknown chat id")
    return _store(key, data)


def _same_picture(path, new_bytes):
    """Same image as the stored file? Exact bytes, or (Pillow) nearly identical at 64x64, so a copy imported at another size is not a change."""
    try:
        with open(path, "rb") as f:
            old = f.read()
        if old == new_bytes:
            return True
        if Image is None:
            return False
        from PIL import ImageChops, ImageStat
        a, b = (Image.open(io.BytesIO(x)).convert("RGB").resize((64, 64), Image.LANCZOS) for x in (old, new_bytes))
        return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 < 6
    except Exception:
        return False


def _store(key, data):
    if not data:
        raise ValueError("empty body")
    if len(data) > MAX_AVATAR:
        raise ValueError("image too large (max 15MB)")
    ext = sniff(data)
    if not ext:
        raise ValueError("unsupported image type (jpeg/png/webp/gif)")
    out_bytes = data
    if Image is not None:
        try:
            im = Image.open(io.BytesIO(data))
            im.seek(0)
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                rgba = im.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[3])
                im = bg
            im = ImageOps.fit(im.convert("RGB"), (SIZE, SIZE), Image.LANCZOS, centering=(0.5, 0.5))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88, optimize=True)
            out_bytes, ext = buf.getvalue(), "jpg"
        except Exception as e:
            raise ValueError(f"cannot decode image: {e}")
    os.makedirs(AVATARS, exist_ok=True)
    cur = current_file(key)
    if cur:
        if _same_picture(os.path.join(AVATARS, cur), out_bytes):  # same picture again: keep file + mtime, no history entry
            return avatar_url(cur)
        archive_current(key)  # a different picture: the old one goes to history first
        _remove_key(key)  # stray duplicates with another extension
    fname = f"{key}.{ext}"
    tmp = os.path.join(AVATARS, f".{fname}.tmp")
    with open(tmp, "wb") as f:
        f.write(out_bytes)
    os.replace(tmp, os.path.join(AVATARS, fname))
    return avatar_url(fname)
