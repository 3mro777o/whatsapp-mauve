"""Serve the viewer's JSON (same shapes as the old static build) straight from archive.db, with an
in-memory cache invalidated through the `changes` table.

  chats.json   [{id,n,g,a,t,p,pf,c,j,jid,alt?,type,parent?,ps,mc?,u}]   (u = unread count)
  c/<id>.json  [message objects]  (+ "ed" previous versions, "del" revocation info, "big"/"gone" media flags)
  search.json  [[chatId, idx, ts, text, flags, fileNames]]  (fileNames absent on previous-version rows)
  media.json   [[chatId, idx, ts, kind, path, poster, w, h, extra, title, filename, ext, mime, type]]
               (indexes 0-8 as before; 9 title|null (links), 10 file name (docs: original name), 11 lowercase extension without dot,
                12 mime|null, 13 type word image|video|audio|document|gif|link|video note)
  people.json  {"<j>": {"n": name, "chat": id|null}}
"""
import json, mimetypes, os, re, threading, time
from collections import defaultdict

import waconf
from archive_db import connect

URL_RE = re.compile(r"https?://[^\s<>\"']+")
PREVIEW = {"img": "📷 صورة", "vid": "🎥 فيديو", "ptt": "🎤 رسالة صوتية", "sticker": "ملصق", "loc": "📍 موقع",
           "poll": "📊 استفتاء"}
CAT = {None: 1, "img": 2, "vid": 3, "ptt": 4, "doc": 5, "sticker": 6}
TYPEWORD = {"img": "image", "vid": "video", "ptt": "audio", "doc": "document", "gif": "gif"}
GROUPISH = ("group", "community", "announce", "broadcast", "status")


def doc_ext(o):
    for cand in (o.get("n"), o.get("m")):
        if cand and "." in cand:
            e = cand.rsplit(".", 1)[1].lower()
            if re.fullmatch(r"[a-z0-9]{1,8}", e):
                return e
    return ""


# ---------- system theme (Omarchy colors.toml; portable: missing file -> None) ----------
_theme = {"checked": 0, "mtime": None, "data": None}


def _theme_path():
    p = (waconf.CONF.get("WA_SYSTEM_THEME") or "").strip()
    return os.path.expanduser(p) if p else None


def theme_mtime():
    """mtime (int) of the theme file, stat'ed at most every 2s; None if disabled/missing."""
    now = time.time()
    if now - _theme["checked"] >= 2:
        _theme["checked"] = now
        p = _theme_path()
        try:
            mt = int(os.stat(p).st_mtime) if p else None
            np = os.path.join(os.path.dirname(os.path.dirname(p)), "theme.name") if p else None
            if np and os.path.exists(np):
                mt = max(mt, int(os.stat(np).st_mtime))
        except (OSError, TypeError):
            mt = None
        if mt != _theme["mtime"]:
            _theme["mtime"], _theme["data"] = mt, None
    return _theme["mtime"]


def theme():
    """{"name","mode","mtime","colors"} or None."""
    mt = theme_mtime()
    if mt is None:
        return None
    if _theme["data"] is None:
        import tomllib
        p = _theme_path()
        try:
            with open(p, "rb") as f:
                raw = tomllib.load(f)
        except (OSError, ValueError):
            return None
        colors = {k: v for k, v in raw.items() if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{3,8}", v)}
        name = None
        np = os.path.join(os.path.dirname(os.path.dirname(p)), "theme.name")
        try:
            name = open(np, encoding="utf-8").read().strip() or None
        except OSError:
            pass
        mode = raw.get("mode") if raw.get("mode") in ("dark", "light") else "dark"
        _theme["data"] = {"name": name, "mode": mode, "mtime": mt, "colors": colors}
    return _theme["data"]


def dumps(x):
    return json.dumps(x, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ViewerData:
    def __init__(self, path=None):
        self.path = path or waconf.DB
        self.lock = threading.RLock()
        self.local = threading.local()
        self.seq = -1
        self.chats = {}          # id -> compiled dict
        self.glob = {}           # name -> bytes
        self.ready = threading.Event()
        self.last_check = 0
        self.full_pending = False
        self.last_full = 0

    def db(self):
        d = getattr(self.local, "db", None)
        if d is None:
            d = self.local.db = connect(self.path, readonly=True)
        return d

    @staticmethod
    def available():
        return os.path.exists(waconf.DB)

    # ---------- names ----------
    def load_names(self):
        db = self.db()
        self.contacts = {r["jid"]: (r["name"], r["push"], r["verified"]) for r in db.execute("select * from contacts")}
        self.chat_rows = {r["id"]: r for r in db.execute("select * from chats")}
        self.id_by_jid = {r["jid"]: r["id"] for r in self.chat_rows.values()}

    def name(self, jid):
        if not jid:
            return None
        if jid == "me":
            return "أنت"
        c = self.contacts.get(jid)
        if c and (c[0] or c[1] or c[2]):
            return c[0] or c[1] or c[2]
        u = jid.split("@")[0]
        return "+" + u if u.isdigit() and jid.endswith("@s.whatsapp.net") else u

    def chat_name(self, r):
        if r["jid"] == "status@broadcast":
            return "الحالات"
        if r["subject"]:
            return r["subject"]
        return self.name(r["jid"])

    # ---------- per chat ----------
    def compile_chat(self, cid):
        db = self.db()
        r = self.chat_rows.get(cid)
        if r is None or r["merged_into"] or r["jid"].endswith("@temp"):
            return None
        ctype = r["type"] or "direct"
        jid = r["jid"]
        cj = jid.split("@")[0]
        groupish = ctype in GROUPISH
        rows = db.execute("select * from messages where chat_id=? order by ts, sort, id", (cid,)).fetchall()
        vers, revs, reacts, med = defaultdict(list), {}, defaultdict(list), {}
        for v in db.execute("""select v.msg_id, v.ts, v.text from message_versions v join messages m on m.id=v.msg_id
                               where m.chat_id=? order by v.ts, v.id""", (cid,)):
            vers[v[0]].append([v[1], v[2]])
        for v in db.execute("""select v.* from revocations v join messages m on m.id=v.msg_id where m.chat_id=?""", (cid,)):
            revs[v["msg_id"]] = v
        for v in db.execute("""select v.msg_id, v.reactor, v.emoji from reactions v join messages m on m.id=v.msg_id
                               where m.chat_id=? and v.emoji != '' order by v.ts""", (cid,)):
            reacts[v[0]].append([v[2], self.name(v[1])])
        for v in db.execute("""select v.msg_id, v.state, v.size, v.mime, v.info is not null has_info from media v
                               join messages m on m.id=v.msg_id where m.chat_id=?""", (cid,)):
            med[v[0]] = v
        msgs, search, media, senders = [], [], [], {}
        ps = defaultdict(lambda: [0] * 8)
        for m in rows:
            k = m["kind"]
            o = {"t": m["ts"]}
            if k:
                o["k"] = k
            if m["from_me"]:
                o["f"] = 1
            elif groupish and m["sender"] and k != "sys":
                o["s"] = self.name(m["sender"])
            if m["text"]:
                o["x"] = m["text"]
            if m["data"]:
                o.update(json.loads(m["data"]))
            mid = m["id"]
            if k == "vid" and "WhatsApp Video Notes" in (o.get("m") or ""):
                o["ptv"] = 1  # imported round video notes (also rows imported before the flag existed)
            if k == "doc":
                o["ext"] = doc_ext(o)
            if k in ("img", "vid", "ptt", "doc", "sticker") and not o.get("m"):
                md = med.get(mid)
                if md is not None and md["has_info"] and md["state"] not in ("done", "auto"):
                    o["big"] = 1
                    o.pop("gone", None)
                    if md["size"] and not o.get("sz"):
                        o["sz"] = md["size"]
                elif md is None or not md["has_info"]:
                    o["gone"] = 1
            if m["edited"]:
                o["e"] = 1
            if mid in vers:
                o["ed"] = vers[mid]
            if m["revoked"]:
                rv = revs.get(mid)
                o["del"] = {"t": rv["deleted_at"] if rv else None,
                            "by": self.name(rv["by"]) if rv and rv["by"] else ("أنت" if m["from_me"] else o.get("s"))}
            if mid in reacts:
                o["r"] = reacts[mid]
            o["id"] = m["key_id"]
            idx = len(msgs)
            msgs.append(o)
            # stats / search / media
            if k != "sys":
                if m["from_me"]:
                    who = "me"
                elif groupish:
                    who = m["sender"].split("@")[0] if m["sender"] else None
                    if who and who not in senders:
                        senders[who] = m["sender"]
                else:
                    who = cj
                if who:
                    st = ps[who]
                    st[0] += 1
                    st[CAT.get(k, 7) if not (k is None and "x" not in o) else 7] += 1
            x = o.get("x")
            if k not in ("sys", "call", "sys2"):
                # flags: 1 = deleted, 2 = edited, 4 = row is a previous version of the text, 8 = sent by me
                fl = (1 if m["revoked"] else 0) | (2 if m["edited"] or mid in vers else 0) | (8 if m["from_me"] else 0)
                # 6th column: media file basename + doc original name, so files match by name (also without a caption)
                fn = " · ".join(dict.fromkeys(p for p in (os.path.basename(o.get("m") or ""), o.get("n") if k == "doc" else None) if p))
                if x or fn:
                    search.append([cid, idx, o["t"], (x or "")[:400], fl, fn])
                for vts, vtext in vers.get(mid, ()):
                    if vtext and vtext != x:
                        search.append([cid, idx, o["t"], vtext[:400], fl | 4])
            wh = o.get("wh") or [None, None]
            if k in ("img", "vid", "ptt", "doc") and (o.get("m") or k == "doc"):
                kk = "gif" if k != "doc" and o.get("gif") else k
                fname = (o.get("n") if k == "doc" else None) or os.path.basename(o.get("m") or "")
                ext = fname.rsplit(".", 1)[1].lower() if "." in fname else ""
                ext = ext if re.fullmatch(r"[a-z0-9]{1,8}", ext) else ""
                mime = (med[mid]["mime"] if mid in med else None) or mimetypes.guess_type(fname)[0]
                tail = [None, fname, ext, mime, "video note" if o.get("ptv") and k == "vid" else TYPEWORD[kk]]
                media.append([cid, idx, o["t"], kk, o.get("m"), o.get("p"), wh[0], wh[1], o.get("n") if k == "doc" else None] + tail)
            elif k is None and x:
                lk = o.get("lk")
                url = lk[2] if lk and lk[2] else None
                if not url:
                    mm = URL_RE.search(x)
                    url = mm.group(0).rstrip(".,;:!?)»”") if mm else None
                if url:
                    media.append([cid, idx, o["t"], "link", None, None, None, None, url, lk[0] if lk and lk[0] else None,
                                  None, "", None, "link"])
        keep_empty = ctype in ("community", "announce", "channel", "status")
        if not any(o.get("k") != "sys" for o in msgs) and not keep_empty:
            return None
        last = msgs[-1] if msgs else {"t": r["created"] or 0}
        preview = last.get("x") or ("📄 " + last.get("n", "مستند") if last.get("k") == "doc" else
                                    PREVIEW.get(last.get("k"), ""))
        summary = {"id": cid, "n": self.chat_name(r), "g": int(jid.endswith("@g.us")), "a": r["archived"] or 0,
                   "t": last["t"], "p": (preview or "")[:120], "pf": last.get("f", 0), "c": len(msgs), "j": cj,
                   "jid": jid, "type": ctype, "ps": dict(ps)}
        if r["alt"]:
            summary["alt"] = r["alt"]
        if r["parent_jid"] and self.id_by_jid.get(r["parent_jid"]):
            summary["parent"] = self.id_by_jid[r["parent_jid"]]
        mc = db.execute("select count(*) from group_members where chat_id=? and active=1", (cid,)).fetchone()[0]
        if mc:
            summary["mc"] = mc
        return {"summary": summary, "json": dumps(msgs), "search": search, "media": media, "senders": senders}

    # ---------- cache control ----------
    def refresh(self, force=False):
        """Bring the cache up to date with the changes feed. Cheap when nothing changed."""
        now = time.time()
        if not force and self.ready.is_set() and now - self.last_check < 1.0:
            return
        with self.lock:
            self.last_check = now
            db = self.db()
            seq = db.execute("select coalesce(max(seq), 0) from changes").fetchone()[0]
            if self.ready.is_set() and seq == self.seq and not force and not self.full_pending:
                return
            if not self.ready.is_set() or force:
                changed, full = None, True
            else:
                changed = {r[0] for r in db.execute("select distinct chat_id from changes where seq > ?", (self.seq,))}
                full = "*" in changed
            if full and self.ready.is_set() and not force and now - self.last_full < 30:
                # name/alias changes: full recompile at most every 30s; meanwhile update the touched chats
                self.full_pending = True
                changed.discard("*")
                full = False
            if self.full_pending and now - self.last_full >= 30:
                full, self.full_pending = True, False
            self.load_names()
            if full:
                self.chats = {}
                for cid in self.chat_rows:
                    c = self.compile_chat(cid)
                    if c:
                        self.chats[cid] = c
                self.last_full = now
            else:
                for cid in changed:
                    c = self.compile_chat(cid) if cid in self.chat_rows else None
                    if c:
                        self.chats[cid] = c
                    else:
                        self.chats.pop(cid, None)
                # parents/renames may have changed
                for cid in list(self.chats):
                    if cid not in self.chat_rows:
                        self.chats.pop(cid)
            self.seq = seq
            self.glob = {}
            self.ready.set()

    def _community_times(self, rows):
        newest = defaultdict(int)
        for c in rows:
            if c.get("parent"):
                newest[c["parent"]] = max(newest[c["parent"]], c["t"])
        for c in rows:
            if c["type"] == "community" and newest.get(c["id"]) and (not c["c"] or c["t"] < newest[c["id"]]):
                c["t"] = newest[c["id"]]

    def get(self, name):
        """name: 'chats', 'search', 'media', 'people' or 'c/<id>'. Returns bytes or None."""
        self.refresh()
        with self.lock:
            if name.startswith("c/"):
                c = self.chats.get(name[2:])
                if c is None:
                    ren = self.db().execute("select new from chat_renames where old=?", (name[2:],)).fetchone()
                    c = self.chats.get(ren[0]) if ren else None
                return c["json"] if c else (b"[]" if name[2:] in self.chat_rows else None)
            if name in self.glob:
                return self.glob[name]
            if name == "chats":
                rows = [dict(c["summary"], u=(self.chat_rows[c["summary"]["id"]]["unread"] or 0) if "unread" in self.chat_rows[c["summary"]["id"]].keys() else 0)
                        for c in self.chats.values()]
                self._community_times(rows)
                rows.sort(key=lambda c: -c["t"])
                out = rows
            elif name == "search":
                out = [r for c in self.chats.values() for r in c["search"]]
                out.sort(key=lambda r: (-r[2],))
            elif name == "media":
                out = [r for c in self.chats.values() for r in c["media"]]
                out.sort(key=lambda r: -r[2])
            elif name == "people":
                out = {}
                for c in self.chats.values():
                    s = c["summary"]
                    if s["type"] == "direct" and s["jid"].split("@")[-1] in ("s.whatsapp.net", "lid"):
                        out[s["j"]] = {"n": s["n"], "chat": s["id"]}
                for c in self.chats.values():
                    for j, jid in c["senders"].items():
                        if j not in out:
                            did = self.id_by_jid.get(jid)
                            out[j] = {"n": self.name(jid), "chat": did if did in self.chats else None}
            else:
                return None
            self.glob[name] = dumps(out)
            return self.glob[name]

    def changes(self, since):
        self.refresh()
        db = self.db()
        seq = db.execute("select coalesce(max(seq), 0) from changes").fetchone()[0]
        if since is None or since <= 0 or since > seq:
            return {"seq": seq, "chats": [], "theme": theme_mtime()}
        rows = {r[0] for r in db.execute("select distinct chat_id from changes where seq > ? limit 5000", (since,))}
        return {"seq": seq, "chats": sorted(rows), "theme": theme_mtime()}

    def members(self, cid):
        self.refresh()
        db = self.db()
        out = []
        for r in db.execute("select member, admin, active from group_members where chat_id=? order by admin desc, member", (cid,)):
            out.append({"j": r["member"].split("@")[0], "n": self.name(r["member"]), "admin": r["admin"],
                        "active": r["active"], "chat": self.id_by_jid.get(r["member"]) if self.id_by_jid.get(r["member"]) in self.chats else None})
        return out

    def chat_ids(self):
        """{chatId: j} for avatars."""
        self.refresh()
        with self.lock:
            return {cid: c["summary"]["j"] for cid, c in self.chats.items()}
