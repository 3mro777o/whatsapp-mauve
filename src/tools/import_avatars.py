#!/usr/bin/env python3
"""Bulk-import chat avatars from avatars-inbox/.

Name each image after the chat name or the phone number, e.g.
  "نور اختي الغاليه.jpg", "4917612345678.png", "+49 176 1234 5678.jpg"
then run `wa-avatars` (or `wa-avatars --watch` to keep polling every 2s).
Matched files are stored in archive/web/user/avatars/ and moved to avatars-inbox/done/.
"""
import json, os, re, shutil, sys, time, unicodedata
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import avatars_lib as av

INBOX = os.path.join(av.ROOT, "avatars-inbox")  # WA_HOME/avatars-inbox
DONE = os.path.join(INBOX, "done")
CHATS = os.path.join(av.WEB, "data", "chats.json")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".jfif"}
DEFAULT_CC = "90"

_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ـ": None})


def norm(s):
    s = unicodedata.normalize("NFKC", s).lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    s = s.translate(_AR)
    s = re.sub(r"[_\-.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def digits(s):
    d = re.sub(r"\D", "", s.strip())
    if s.strip().startswith("+"):
        return d
    if d.startswith("00"):
        return d[2:]
    if d.startswith("0"):
        return DEFAULT_CC + d[1:]
    return d


def load_chats():
    if os.path.exists(av.waconf.DB):
        import viewer_data
        return json.loads(viewer_data.ViewerData().get("chats"))
    with open(CHATS, encoding="utf-8") as f:
        return json.load(f)


def match(stem, chats):
    """Return (chat, how) or (None, reason)."""
    raw = stem.strip()
    if re.fullmatch(r"[\d\s+()\-.]{6,}", raw):
        d = digits(raw)
        hits = [c for c in chats if c.get("j") and re.sub(r"\D", "", c["j"]) == d]
        if not hits and len(d) >= 7:
            hits = [c for c in chats if c.get("j", "").isdigit() and c["j"].endswith(d[-9:])]
        if len(hits) == 1:
            return hits[0], "number"
        if hits:
            return None, f"رقم مطابق لـ{len(hits)} محادثات"
        # fall through: maybe a chat name that is a number
    key = norm(raw)
    if not key:
        return None, "اسم فارغ"
    exact = [c for c in chats if norm(c["n"]) == key]
    if len(exact) == 1:
        return exact[0], "name"
    if len(exact) > 1:
        # prefer the non-group / most recent
        return None, "الاسم مطابق لأكثر من محادثة: " + "، ".join(f'{c["n"]} (#{c["id"]})' for c in exact[:5])
    sub = [c for c in chats if key in norm(c["n"]) or (len(norm(c["n"])) >= 3 and norm(c["n"]) in key)]
    if len(sub) == 1:
        return sub[0], "partial"
    if sub:
        return None, "غامض، مطابق لـ: " + "، ".join(f'{c["n"]}' for c in sub[:5])
    return None, "ما في محادثة بهالاسم/الرقم"


def explicit_id(stem):
    m = re.fullmatch(r"(?:id|#)\s*(\d+)", stem.strip(), re.I)
    return m.group(1) if m else None


def run_once(verbose=True):
    os.makedirs(DONE, exist_ok=True)
    files = sorted(f for f in os.listdir(INBOX)
                   if os.path.isfile(os.path.join(INBOX, f)) and os.path.splitext(f)[1].lower() in IMG_EXT
                   and not f.startswith("."))
    if not files:
        if verbose:
            print("ما في صور بمجلد avatars-inbox/")
        return 0, []
    chats = load_chats()
    by_id = {c["id"]: c for c in chats}
    ok, bad = 0, []
    for f in files:
        src = os.path.join(INBOX, f)
        stem = os.path.splitext(f)[0]
        cid = explicit_id(stem)
        chat, how = (by_id.get(cid), "id") if cid else match(stem, chats)
        if not chat:
            bad.append((f, how if not cid else "رقم المحادثة غير موجود"))
            continue
        try:
            with open(src, "rb") as fh:
                data = fh.read()
            av.save_avatar(chat["id"], data)
        except (ValueError, OSError) as e:
            bad.append((f, f"خطأ بالصورة: {e}"))
            continue
        dst = os.path.join(DONE, f)
        if os.path.exists(dst):
            dst = os.path.join(DONE, f"{stem}.{int(time.time())}{os.path.splitext(f)[1]}")
        shutil.move(src, dst)
        ok += 1
        print(f"✓ {f}  →  {chat['n']}  (#{chat['id']}, {how})")
    if bad:
        print("\nما انطابقت:")
        for f, why in bad:
            print(f"✗ {f}  —  {why}")
        print("نصيحة: سمّي الملف بالاسم كامل متل ما هو بالقائمة، أو بالرقم الدولي، أو id123.jpg برقم المحادثة.")
    print(f"\n{ok} صورة انضافت، {len(bad)} ما انطابقت. حدّث صفحة العارض لتشوفها.")
    return ok, bad


def main():
    os.makedirs(DONE, exist_ok=True)
    if "--watch" in sys.argv:
        sys.stdout.reconfigure(line_buffering=True)
        print(f"مراقبة {INBOX} كل ثانيتين… (Ctrl+C للإيقاف)")
        seen_bad = set()
        try:
            while True:
                pending = {f for f in os.listdir(INBOX)
                           if os.path.splitext(f)[1].lower() in IMG_EXT and not f.startswith(".")}
                # wait until new files stop growing
                new = pending - seen_bad
                if new:
                    sizes = {f: os.path.getsize(os.path.join(INBOX, f)) for f in new}
                    time.sleep(0.5)
                    if all(os.path.exists(os.path.join(INBOX, f)) and
                           os.path.getsize(os.path.join(INBOX, f)) == s for f, s in sizes.items()):
                        _, bad = run_once(verbose=False)
                        seen_bad = {f for f, _ in bad}
                seen_bad &= pending
                time.sleep(2)
        except KeyboardInterrupt:
            print()
    else:
        run_once()


if __name__ == "__main__":
    main()
