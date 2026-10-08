#!/usr/bin/env python3
"""Build archive/web/data/* (compact per-chat JSON) from the decrypted msgstore.db.

Names come from archive/chats.json (wtsexporter output, already enriched from contacts.vcf).
"""
import array, hashlib, json, re, os, sqlite3, shutil, struct, subprocess, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/tools -> repo root
OUT = os.path.join(ROOT, "archive/web/data")
THUMBS = os.path.join(ROOT, "archive/web/thumbs")  # NOT wiped by builds (cache)

# WhatsApp Android message_system.action_type -> Arabic label ({a}=actor, {p}=participants)
SYSTEM = {
    1: "{a} غيّر اسم المجموعة إلى «{t}»", 4: "{a} انضم", 5: "{a} غادر",
    6: "{a} غيّر صورة المجموعة", 10: "{a} غيّر رقمه", 11: "{a} أنشأ المجموعة «{t}»",
    12: "{a} أضاف {p}", 13: "{a} أزال {p}", 14: "{a} أزال {p}", 15: "{a} صار مشرفاً",
    18: "تغيّر رمز الأمان لـ {a}", 19: "🔒 الرسائل مشفّرة تماماً بين الطرفين",
    20: "{a} انضم عبر رابط الدعوة", 27: "{a} غيّر وصف المجموعة", 28: "{a} غيّر رقمه",
    58: "{a} غيّر إعدادات الرسائل المؤقتة", 67: "🔒 الرسائل والمكالمات مشفّرة تماماً بين الطرفين",
    69: "🔒 تغيّر رمز الأمان", 129: "{a} غيّر إعدادات المجموعة",
}
CALL_RESULT = {2: "مكالمة فائتة", 5: "مكالمة", 3: "مكالمة مرفوضة", 4: "مكالمة لم يُرد عليها"}


def jpeg_size(path):
    """(w, h) of a JPEG by scanning for its SOF marker, or None."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                while b == b"\xff":
                    b = f.read(1)
                if not b:
                    return None
                marker = b[0]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    continue
                seg = f.read(2)
                if len(seg) < 2:
                    return None
                ln = struct.unpack(">H", seg)[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">xHH", f.read(5))
                    return (w, h) if w and h else None
                f.seek(ln - 2, 1)
    except OSError:
        return None


def make_thumb(src, dst):
    """Poster frame for a video/GIF via ffmpeg. Returns True if dst exists afterwards."""
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return True
    tmp = dst + ".tmp.jpg"
    for ss in ("0.5", "0"):
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", ss, "-i", src, "-frames:v", "1",
                            "-vf", "scale='min(480,iw)':-2", "-q:v", "5", tmp],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            pass
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def add_video_thumbs(vids):
    """vids: list of message dicts (k=="vid" with "m"). Adds "p" (+ "wh" if missing)."""
    if not vids:
        return
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found: skipping video thumbnails", file=sys.stderr)
        return
    os.makedirs(THUMBS, exist_ok=True)
    jobs = {}
    for o in vids:
        name = hashlib.sha1(o["m"].encode("utf-8")).hexdigest() + ".jpg"
        jobs.setdefault(name, []).append(o)
    todo = [n for n in jobs if not os.path.exists(os.path.join(THUMBS, n))]
    t0, done, made = time.time(), 0, 0
    if todo:
        print(f"video thumbnails: {len(jobs)} total, {len(todo)} to generate")
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            futs = {ex.submit(make_thumb, os.path.join(ROOT, jobs[n][0]["m"]), os.path.join(THUMBS, n)): n
                    for n in todo}
            for fu in as_completed(futs):
                done += 1
                made += bool(fu.result())
                if done % 100 == 0 or done == len(todo):
                    print(f"\r  {done}/{len(todo)} ({made} ok, {time.time() - t0:.0f}s)", end="", flush=True)
        print()
    ok = 0
    for n, msgs in jobs.items():
        path = os.path.join(THUMBS, n)
        if not os.path.exists(path):
            continue
        ok += 1
        wh = None
        for o in msgs:
            o["p"] = "thumbs/" + n
            if "wh" not in o:
                wh = wh or jpeg_size(path)
                if wh:
                    o["wh"] = list(wh)
    print(f"video thumbnails: {ok}/{len(jobs)} available ({made} new in {time.time() - t0:.1f}s)")


WAVE_BARS = 48
WAVE_ALPHABET = "0123456789abcdefghijklmnopqrstuv"  # bar value 0..31 -> one char
WAVE_CACHE = os.path.join(THUMBS, "waves.json")  # {path: [size, mtime, w, duration]}


def compute_wave(src):
    """Decode to mono 8 kHz s16le via ffmpeg; return (w_string, duration_seconds) or None."""
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "8000",
                            "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    raw = r.stdout[:len(r.stdout) // 2 * 2]
    if not raw:
        return None
    a = array.array("h")
    a.frombytes(raw)
    if sys.byteorder != "little":
        a.byteswap()
    n = len(a)
    peaks = []
    for i in range(WAVE_BARS):
        chunk = a[i * n // WAVE_BARS:(i + 1) * n // WAVE_BARS]
        peaks.append(max(max(chunk), -min(chunk)) if chunk else 0)
    top = max(peaks) or 1
    w = "".join(WAVE_ALPHABET[min(31, round(p * 31 / top))] for p in peaks)
    return w, max(1, round(n / 8000))


def add_waveforms(msgs):
    """msgs: ptt/audio message dicts with "m". Adds "w" and fills "d" if missing."""
    if not msgs or not shutil.which("ffmpeg"):
        if msgs:
            print("ffmpeg not found: skipping waveforms", file=sys.stderr)
        return
    try:
        with open(WAVE_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    stat = {}
    for o in msgs:
        if o["m"] not in stat:
            try:
                st = os.stat(os.path.join(ROOT, o["m"]))
                stat[o["m"]] = [st.st_size, int(st.st_mtime)]
            except OSError:
                pass
    todo = [p for p, s in stat.items() if (cache.get(p) or [None, None])[:2] != s]
    t0, made = time.time(), 0
    if todo:
        print(f"waveforms: {len(stat)} total, {len(todo)} to compute")
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            futs = {ex.submit(compute_wave, os.path.join(ROOT, p)): p for p in todo}
            for i, fu in enumerate(as_completed(futs), 1):
                p, res = futs[fu], fu.result()
                if res:
                    cache[p] = stat[p] + list(res)
                    made += 1
                else:
                    cache.pop(p, None)
                if i % 200 == 0 or i == len(todo):
                    print(f"\r  {i}/{len(todo)} ({made} ok, {time.time() - t0:.0f}s)", end="", flush=True)
        print()
    cache = {p: v for p, v in cache.items() if p in stat}
    os.makedirs(THUMBS, exist_ok=True)
    tmp = WAVE_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    os.replace(tmp, WAVE_CACHE)
    ok = 0
    for o in msgs:
        v = cache.get(o["m"])
        if v:
            ok += 1
            o["w"] = v[2]
            if not o.get("d"):
                o["d"] = v[3]
        if o.get("d"):
            o["d"] = int(o["d"])
    print(f"waveforms: {ok}/{len(msgs)} messages ({made} new in {time.time() - t0:.1f}s)")


IMG_DOC_EXT = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif"}


def doc_ext(o):
    for cand in (o.get("n"), o.get("m")):
        if cand and "." in cand:
            e = cand.rsplit(".", 1)[1].lower()
            if re.fullmatch(r"[a-z0-9]{1,8}", e):
                return e
    return ""


def make_doc_thumb(src, dst, ext):
    """First-page JPEG for PDFs (pdftoppm) / scaled JPEG for images sent as documents."""
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return True
    tmp = dst + ".tmp"
    try:
        if ext == "pdf":
            if not shutil.which("pdftoppm"):
                return False
            subprocess.run(["pdftoppm", "-jpeg", "-r", "50", "-f", "1", "-l", "1", "-scale-to", "480",
                            "-singlefile", src, tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60)
            tmp += ".jpg"  # pdftoppm appends the extension
        else:
            try:
                from PIL import Image, ImageOps
                im = ImageOps.exif_transpose(Image.open(src))
                im.thumbnail((480, 480 * 4))
                if im.mode not in ("RGB", "L"):
                    rgba = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[3])
                    im = bg
                im.convert("RGB").save(tmp, "JPEG", quality=80)
            except Exception:  # e.g. HEIC without a Pillow plugin -> ffmpeg
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-frames:v", "1",
                                "-vf", "scale='min(480,iw)':-2", "-q:v", "5", "-f", "image2", tmp],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        pass
    if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, dst)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def pdf_pages(src):
    try:
        out = subprocess.run(["pdfinfo", src], capture_output=True, text=True, timeout=30).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else None
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        return None


def add_doc_previews(docs):
    """docs: all k=="doc" messages. Adds "ext"; for PDFs / image files also "p", "wh" and (PDF) "pg"."""
    t0 = time.time()
    jobs = {}
    for o in docs:
        o["ext"] = doc_ext(o)
        if o.get("m") and (o["ext"] == "pdf" or o["ext"] in IMG_DOC_EXT):
            jobs.setdefault(hashlib.sha1(o["m"].encode("utf-8")).hexdigest() + ".jpg", []).append(o)
    if not jobs:
        return
    os.makedirs(THUMBS, exist_ok=True)
    need_pages = {n for n, ms in jobs.items() if ms[0]["ext"] == "pdf" and any(not o.get("pg") for o in ms)}
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        thumbs = {n: ex.submit(make_doc_thumb, os.path.join(ROOT, ms[0]["m"]), os.path.join(THUMBS, n), ms[0]["ext"])
                  for n, ms in jobs.items()}
        pages = {n: ex.submit(pdf_pages, os.path.join(ROOT, jobs[n][0]["m"])) for n in need_pages}
        counts = {"pdf": 0, "img": 0}
        for n, fu in thumbs.items():
            ok = fu.result()
            pg = pages[n].result() if n in pages else None
            for o in jobs[n]:
                if pg and not o.get("pg"):
                    o["pg"] = pg
                if ok:
                    o["p"] = "thumbs/" + n
                    wh = jpeg_size(os.path.join(THUMBS, n))
                    if wh:
                        o["wh"] = list(wh)
            if ok:
                counts["pdf" if jobs[n][0]["ext"] == "pdf" else "img"] += 1
    total = {k: sum(1 for ms in jobs.values() if (ms[0]["ext"] == "pdf") == (k == "pdf")) for k in counts}
    print(f"doc previews: pdf {counts['pdf']}/{total['pdf']}, image-docs {counts['img']}/{total['img']} "
          f"({time.time() - t0:.1f}s)")


URL_RE = re.compile(r"https?://[^\s<>\"']+")


def write_media_index(pending, path):
    """media.json rows (newest first):
    [chatId, msgIndex, ts, kind, path|null, poster|null, w|null, h|null, extra|null(, title)]"""
    rows = []
    for cid, msgs in pending:
        for i, o in enumerate(msgs):
            k = o.get("k")
            wh = o.get("wh") or [None, None]
            base = [cid, i, o["t"]]
            if k in ("img", "vid", "ptt") and o.get("m"):
                kind = "gif" if o.get("gif") else k
                rows.append(base + [kind, o["m"], o.get("p"), wh[0], wh[1], None])
            elif k == "doc":
                rows.append(base + ["doc", o.get("m"), o.get("p"), wh[0], wh[1], o.get("n")])
            elif k is None:
                lk = o.get("lk")
                url = lk[2] if lk and lk[2] else None
                if not url:
                    mm = URL_RE.search(o.get("x") or "")
                    url = mm.group(0).rstrip(".,;:!?)»”") if mm else None
                if url:
                    row = base + ["link", None, None, None, None, url]
                    if lk and lk[0]:
                        row.append(lk[0])
                    rows.append(row)
    rows.sort(key=lambda r: -r[2])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
    return rows


def main():
    names = {}
    for jid, c in json.load(open(os.path.join(ROOT, "archive/chats.json"), encoding="utf-8")).items():
        if c.get("name") and c["name"] != jid.split("@")[0]:
            names[jid] = c["name"]

    db = sqlite3.connect(os.path.join(ROOT, "msgstore.db"))
    db.row_factory = sqlite3.Row
    jids = {r["_id"]: r["raw_string"] for r in db.execute("select _id, raw_string from jid")}
    # LID jids (@lid) map to phone jids
    alt_jid = {}  # lid jid <-> phone jid (for avatar fetching)
    for r in db.execute("select lid_row_id, jid_row_id from jid_map") if db.execute(
            "select 1 from sqlite_master where name='jid_map'").fetchone() else []:
        if r[0] in jids and r[1] in jids:
            alt_jid[jids[r[0]]] = jids[r[1]]
            alt_jid[jids[r[1]]] = jids[r[0]]
            names.setdefault(jids[r[0]], names.get(jids[r[1]]) or "+" + jids[r[1]].split("@")[0])

    def name(jid_row):
        j = jids.get(jid_row)
        if not j:
            return None
        return names.get(j) or ("+" + j.split("@")[0] if j.split("@")[0].isdigit() else j.split("@")[0])

    def lookup(table, key="message_row_id"):
        return {r[key]: r for r in db.execute(f"select * from {table}")}

    media = lookup("message_media")
    quoted = lookup("message_quoted")
    system = lookup("message_system")
    location = lookup("message_location")
    edited = lookup("message_edit_info")
    text_extra = lookup("message_text")
    calls = {r["message_row_id"]: r for r in db.execute(
        "select mc.message_row_id, c.* from message_call_log mc join call_log c on c._id = mc.call_log_row_id")}
    participants = defaultdict(list)
    for r in db.execute("select * from message_system_chat_participant"):
        participants[r["message_row_id"]].append(r["user_jid_row_id"])
    polls = defaultdict(list)
    for r in db.execute("select message_row_id, option_name, vote_total from message_poll_option order by _id"):
        polls[r[0]].append([r[1], r[2] or 0])
    reactions = defaultdict(list)
    for r in db.execute("""select a.parent_message_row_id p, a.from_me, a.sender_jid_row_id s, r.reaction
                           from message_add_on a join message_add_on_reaction r on r.message_add_on_row_id = a._id
                           where r.reaction is not null and r.reaction != ''"""):
        reactions[r["p"]].append([r["reaction"], "أنت" if r["from_me"] else name(r["s"])])

    # ---- chat types / community structure ----
    chat_rows = {r["_id"]: r for r in db.execute(
        "select c._id, c.group_type, c.created_timestamp, j.raw_string jid from chat c join jid j on j._id = c.jid_row_id")}
    chat_of_jidrow = {r[0]: r[1] for r in db.execute("select jid_row_id, _id from chat")}
    # message_system_with_group_nodes: node type 1 = community parent, 2 = subgroup (incl. announcements)
    parent_of, nodes = {}, defaultdict(list)
    for r in db.execute("""select g.message_row_id, g.group_jid_row_id, g.group_node_type, m.chat_row_id
                           from message_system_with_group_nodes g join message m on m._id = g.message_row_id"""):
        nodes[(r[0], r[3])].append((chat_of_jidrow.get(r[1]), r[2]))
    for (_, host), ns in nodes.items():
        par = next((c for c, nt in ns if nt == 1 and c), None)
        if not par:
            continue
        for c, nt in ns:
            if nt == 2 and c and c != par:
                parent_of[c] = par
        if host != par and chat_rows.get(host) and chat_rows[host]["group_type"] in (2, 3, 6):
            parent_of.setdefault(host, par)
    status_posts = {r[0] for r in db.execute("select message_row_id from status_message_info")}
    newsletter_names = {}
    if db.execute("select 1 from sqlite_master where name='newsletter'").fetchone():
        cols = [r[1] for r in db.execute("pragma table_info(newsletter)")]
        if "name" in cols and "chat_row_id" in cols:
            newsletter_names = {r[0]: r[1] for r in db.execute("select chat_row_id, name from newsletter") if r[1]}

    def chat_type(cid, jid):
        if jid == "status@broadcast":
            return "status"
        if jid.endswith("@broadcast"):
            return "broadcast"
        if jid.endswith("@newsletter"):
            return "channel"
        if jid.endswith("@g.us"):
            gt = chat_rows[cid]["group_type"] if cid in chat_rows else 0
            return {1: "community", 3: "announce"}.get(gt, "group")
        return "direct"

    def user_key(jid_row):
        j = jids.get(jid_row)
        return j.split("@")[0] if j else None

    people = {}  # j -> {"n": name, "chat": direct chat id or None}
    CAT = {None: 1, "img": 2, "vid": 3, "ptt": 4, "doc": 5, "sticker": 6}

    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(os.path.join(OUT, "c"))
    index, search, pending, vids, auds = [], [], [], [], []

    for chat in db.execute("""select c._id, c.subject, c.archived, j.raw_string jid from chat c
                              join jid j on j._id = c.jid_row_id"""):
        jid = chat["jid"]
        is_group = jid.endswith("@g.us")
        ctype = chat_type(chat["_id"], jid)
        cj = jid.split("@")[0]
        pstats = defaultdict(lambda: [0] * 8)
        cname = chat["subject"] or names.get(jid) or newsletter_names.get(chat["_id"]) or (
            "+" + cj if cj.isdigit() and ctype == "direct" else cj)
        if jid == "status@broadcast":
            cname = "الحالات"
        msgs, key_to_idx = [], {}
        rows = db.execute("""select * from message where chat_row_id = ? and timestamp > 0
                             order by sort_id, timestamp""", (chat["_id"],)).fetchall()
        for m in rows:
            mid, t = m["_id"], m["message_type"]
            o = {"t": int(m["timestamp"] / 1000)}
            if m["from_me"]:
                o["f"] = 1
                o["st"] = m["status"]
            elif is_group and m["sender_jid_row_id"]:
                o["s"] = name(m["sender_jid_row_id"])
            text = m["text_data"]

            if t == 7:  # system event
                s = system.get(mid)
                at = s["action_type"] if s else None
                actor = "أنت" if m["from_me"] else (name(m["sender_jid_row_id"]) or cname)
                ps = "، ".join(filter(None, (name(p) for p in participants.get(mid, [])))) or ""
                tpl = SYSTEM.get(at)
                if not tpl:
                    continue  # unknown internal event: hide instead of "Not supported"
                o = {"t": o["t"], "k": "sys", "x": tpl.format(a=actor, p=ps, t=text or "")}
            elif t == 90 or mid in calls:
                c = calls.get(mid)
                if not c:
                    continue
                o["k"] = "call"
                o["v"] = c["video_call"]
                o["d"] = c["duration"]
                o["x"] = ("مكالمة فيديو" if c["video_call"] else "مكالمة صوتية") if c["duration"] else \
                    ("مكالمة فائتة" if not c["from_me"] else "لم يتم الرد")
                o["f"] = c["from_me"]
            elif t == 15:
                o["k"] = "del"
                o["x"] = "تم حذف هذه الرسالة"
            elif t == 99 or mid in polls:
                o["k"] = "poll"
                o["x"] = text or ""
                o["o"] = polls.get(mid, [])
            elif mid in location:
                l = location[mid]
                o["k"] = "loc"
                o["x"] = l["place_name"] or ("موقع مباشر" if t == 16 else "موقع")
                o["ll"] = [l["latitude"], l["longitude"]]
            elif mid in media:
                md = media[mid]
                path = md["file_path"]
                mime = md["mime_type"] or ""
                o["k"] = ("sticker" if t == 20 else "img" if mime.startswith("image") or t in (1, 42)
                          else "vid" if mime.startswith("video") or t in (3, 13, 43) else
                          "ptt" if t in (2, 81, 82) or mime.startswith("audio") else "doc")
                if t == 13:
                    o["gif"] = 1
                if path and os.path.exists(os.path.join(ROOT, path)):
                    o["m"] = path
                if md["width"] and md["height"]:
                    o["wh"] = [md["width"], md["height"]]
                if o["k"] == "doc":
                    o["n"] = md["media_name"] or (os.path.basename(path) if path else "مستند")
                    o["sz"] = md["file_size"]
                    if md["page_count"]:
                        o["pg"] = md["page_count"]
                if md["media_duration"]:
                    o["d"] = md["media_duration"]
                if text or md["media_caption"]:
                    o["x"] = text or md["media_caption"]
            elif text is None and t not in (0,):
                if t in (64, 42, 43, 81, 82):
                    o["k"] = "sys2"
                    o["x"] = "📷 رسالة للعرض مرة واحدة"
                elif t == 4:
                    o["x"] = "👤 جهة اتصال"
                else:
                    continue  # unsupported internal type: skip
            else:
                o["x"] = text or ""
                te = text_extra.get(mid)
                if te and te["url"]:
                    o["lk"] = [te["page_title"] or "", te["description"] or "", te["url"]]

            q = quoted.get(mid)
            if q:
                qm = media.get(mid)
                o["q"] = [q["key_id"], "أنت" if q["from_me"] else (name(q["sender_jid_row_id"]) or cname),
                          q["text_data"] or {1: "📷 صورة", 3: "🎥 فيديو", 2: "🎤 رسالة صوتية", 9: "📄 مستند",
                                             20: "ملصق"}.get(q["message_type"], "رسالة")]
            if mid in edited:
                o["e"] = 1
            if mid in reactions:
                o["r"] = reactions[mid]
            o["id"] = m["key_id"]
            if mid in status_posts:
                o["gs"] = 1  # group status post
            key_to_idx[m["key_id"]] = len(msgs)
            msgs.append(o)
            if o.get("k") != "sys":
                if m["from_me"]:
                    who = "me"
                elif ctype in ("group", "community", "announce", "broadcast") or (is_group and m["sender_jid_row_id"]):
                    who = user_key(m["sender_jid_row_id"])
                    if who and who not in people:
                        people[who] = {"n": name(m["sender_jid_row_id"]), "chat": None}
                else:
                    who = cj
                if who:
                    st = pstats[who]
                    st[0] += 1
                    k = o.get("k")
                    st[CAT.get(k, 7) if not (k is None and "x" not in o) else 7] += 1
            if o.get("x") and o.get("k") not in ("sys", "call", "del", "sys2"):
                search.append([str(chat["_id"]), len(msgs) - 1, o["t"], o["x"][:400]])

        keep_empty = ctype in ("community", "announce", "channel", "status")
        if not any(m.get("k") != "sys" for m in msgs) and not keep_empty:
            search[:] = [r for r in search if r[0] != str(chat["_id"])]
            continue
        if ctype == "direct" and chat["jid"].split("@")[1] in ("s.whatsapp.net", "lid"):
            people[cj] = {"n": cname, "chat": str(chat["_id"])}
        created = int((chat_rows[chat["_id"]]["created_timestamp"] or 0) / 1000) if chat["_id"] in chat_rows else 0
        last = msgs[-1] if msgs else {"t": created}
        preview = last.get("x") or {"img": "📷 صورة", "vid": "🎥 فيديو", "ptt": "🎤 رسالة صوتية",
                                     "doc": "📄 " + last.get("n", "مستند"), "sticker": "ملصق"}.get(last.get("k"), "")
        cid = str(chat["_id"])
        pending.append((cid, msgs))
        vids.extend(m for m in msgs if m.get("k") == "vid" and m.get("m"))
        auds.extend(m for m in msgs if m.get("k") == "ptt" and m.get("m"))
        index.append({"id": cid, "n": cname, "g": int(is_group), "a": chat["archived"] or 0,
                      "t": last["t"], "p": preview[:120], "pf": last.get("f", 0), "c": len(msgs),
                      "j": jid.split("@")[0], "jid": jid, **({"alt": alt_jid[jid]} if jid in alt_jid else {}),
                      "type": ctype, **({"parent": str(parent_of[chat["_id"]])} if chat["_id"] in parent_of else {}),
                      "ps": dict(pstats)})

    add_video_thumbs(vids)
    add_waveforms(auds)
    add_doc_previews([m for _, msgs in pending for m in msgs if m.get("k") == "doc"])
    for cid, msgs in pending:
        with open(os.path.join(OUT, "c", cid + ".json"), "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False, separators=(",", ":"))

    rows = write_media_index(pending, os.path.join(OUT, "media.json"))
    kinds = defaultdict(int)
    for r in rows:
        kinds[r[3]] += 1
    print("media.json:", len(rows), "rows", dict(kinds))

    # community parents without messages: show them after their newest subgroup
    ids = {c["id"] for c in index}
    for c in index:
        if c.get("parent") and c["parent"] not in ids:
            c.pop("parent")  # parent chat missing from the DB entirely
    newest = defaultdict(int)
    for c in index:
        if c.get("parent"):
            newest[c["parent"]] = max(newest[c["parent"]], c["t"])
    for c in index:
        if c["type"] == "community" and not c["c"] and newest.get(c["id"]):
            c["t"] = newest[c["id"]]

    # people.json: j -> {"n": display name, "chat": 1:1 chat id or null}; lid <-> phone share the 1:1 chat
    for j, v in people.items():
        if not v["chat"]:
            for dom in ("@lid", "@s.whatsapp.net"):
                alt = alt_jid.get(j + dom)
                if alt and people.get(alt.split("@")[0], {}).get("chat"):
                    v["chat"] = people[alt.split("@")[0]]["chat"]
                    break
        if not v["n"]:
            v["n"] = "+" + j if j.isdigit() else j
    with open(os.path.join(OUT, "people.json"), "w", encoding="utf-8") as f:
        json.dump(people, f, ensure_ascii=False, separators=(",", ":"))

    index.sort(key=lambda c: -c["t"])
    with open(os.path.join(OUT, "chats.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT, "search.json"), "w", encoding="utf-8") as f:
        json.dump(search, f, ensure_ascii=False, separators=(",", ":"))
    shutil.copy(os.path.join(ROOT, "src/tools/viewer.html"), os.path.join(ROOT, "archive/web/index.html"))
    shutil.copytree(os.path.join(ROOT, "src/tools/fonts"), os.path.join(ROOT, "archive/web/fonts"), dirs_exist_ok=True)
    media_link = os.path.join(ROOT, "archive/web/Media")
    if not os.path.lexists(media_link):
        os.symlink("../../Media", media_link)
    print(f"{len(index)} chats, {len(search)} searchable messages")


if __name__ == "__main__":
    main()
