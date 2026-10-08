"""archive.db access layer: the single writer-side implementation of the merge rules.

Used by the backup importer (import_msgstore.py), the live-sync writer (archive_writer.py, fed by
wa-sync) and the enrichment step. Rules:
  * rows are never deleted, except by purge_messages() (explicit user action), which leaves a tombstone in `purged`
    that upsert_message() honors (live sync, history sync and the backup importer all insert through it);
  * a revoked message keeps its last known content (+ a row in `revocations`);
  * a changed text/caption pushes the previous one into `message_versions`;
  * every change appends the chat id to `changes` (viewer live-update feed).
"""
import sys, hashlib, json, os, re, sqlite3, time

import waconf

SCHEMA = os.path.join(waconf.TOOLS, "schema.sql")
MEDIA_KINDS_ENRICH = ("vid", "ptt", "doc")


def connect(path=None, readonly=False):
    path = path or waconf.DB
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        db = sqlite3.connect(path, timeout=60, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=60000")
    db.row_factory = sqlite3.Row
    return db


def norm_jid(jid):
    """Strip device suffix (user:12@server) and legacy servers."""
    if not jid:
        return jid
    jid = str(jid)
    if "@" not in jid:
        return jid
    user, srv = jid.split("@", 1)
    user = user.split(":", 1)[0]
    if srv == "c.us":
        srv = "s.whatsapp.net"
    return f"{user}@{srv}"


def chat_type_for(jid, group_type=0, meta=None):
    if jid == "status@broadcast":
        return "status"
    srv = jid.split("@", 1)[-1]
    if srv == "broadcast":
        return "broadcast"
    if srv == "newsletter":
        return "channel"
    if srv == "g.us":
        meta = meta or {}
        if meta.get("isCommunity") or group_type == 1:
            return "community"
        if meta.get("isCommunityAnnounce") or group_type == 3:
            return "announce"
        return "group"
    return "direct"


def migrate(db):
    """Add columns that older archive.db files lack (idempotent, safe against a concurrent migrator)."""
    cols = {r[1] for r in db.execute("pragma table_info(chats)")}
    for c, t in (("unread", "INTEGER DEFAULT 0"), ("read_ts", "INTEGER")):
        if c not in cols:
            try:
                db.execute(f"alter table chats add column {c} {t}")
            except sqlite3.OperationalError:
                pass
    db.commit()


class Archive:
    def __init__(self, path=None):
        self.db = connect(path)
        with open(SCHEMA, encoding="utf-8") as f:
            self.db.executescript(f.read())
        migrate(self.db)
        self.alias = {r[0]: r[1] for r in self.db.execute("select jid, canon from aliases")}
        self.chat_by_jid = {r[0]: r[1] for r in self.db.execute("select jid, id from chats")}
        self.ids = set(self.chat_by_jid.values())
        self.touched = set()

    # ---------- identities ----------
    def canon(self, jid):
        j = norm_jid(jid)
        return self.alias.get(j, j)

    def _new_id(self, jid):
        if jid == "status@broadcast":
            base = "status"
        else:
            user, srv = jid.split("@", 1) if "@" in jid else (jid, "")
            base = ("bc" + user) if srv == "broadcast" else user
            base = re.sub(r"[^A-Za-z0-9_-]", "_", base)[:64] or "chat"
        cid, n = base, 1
        while cid in self.ids:
            n += 1
            cid = f"{base}_{n}"
        return cid

    def ensure_chat(self, jid, **f):
        """Create/update a chat by jid; returns its id. Only non-None fields are written."""
        jid = self.canon(jid)
        now = int(time.time())
        cid = self.chat_by_jid.get(jid)
        meta = f.pop("meta", None)
        f = {k: v for k, v in f.items() if v is not None}
        if cid is None:
            cid = self._new_id(jid)
            f.setdefault("type", chat_type_for(jid, f.get("group_type") or 0, meta))
            cols = ["id", "jid", "updated"] + list(f) + (["meta"] if meta else [])
            vals = [cid, jid, now] + list(f.values()) + ([json.dumps(meta, ensure_ascii=False)] if meta else [])
            self.db.execute(f"insert into chats ({','.join(cols)}) values ({','.join('?' * len(cols))})", vals)
            self.chat_by_jid[jid] = cid
            self.ids.add(cid)
            self.touched.add(cid)
            return cid
        if meta:
            old = self.db.execute("select meta from chats where id=?", (cid,)).fetchone()[0]
            merged = {**(json.loads(old) if old else {}), **meta}
            f["meta"] = json.dumps(merged, ensure_ascii=False)
        if f:
            cur = self.db.execute("select * from chats where id=?", (cid,)).fetchone()
            diff = {k: v for k, v in f.items() if cur[k] != v}
            if diff:
                diff["updated"] = now
                self.db.execute(f"update chats set {','.join(k + '=?' for k in diff)} where id=?",
                                list(diff.values()) + [cid])
                self.touched.add(cid)
        return cid

    def set_unread(self, cid, n=None, inc=0, clamp=False):
        """Absolute (n) or relative (inc) unread count. clamp: a count from WhatsApp is capped to the incoming messages
        newer than our own last local read, so reopening a chat here is not undone by a reconnect."""
        cur = self.db.execute("select unread, read_ts from chats where id=?", (cid,)).fetchone()
        if cur is None:
            return
        old = cur[0] or 0
        n = max(old + inc if n is None else int(n), 0)
        if n and clamp and cur[1]:
            n = min(n, self.db.execute("select count(*) from messages where chat_id=? and from_me=0 and ts>=? and kind is not 'sys'",
                                       (cid, cur[1])).fetchone()[0])
        if n == old:
            return
        self.db.execute("update chats set unread=?, read_ts=? where id=?", (n, int(time.time()) if n == 0 else cur[1], cid))
        self.touched.add(cid)

    def set_alias(self, lid, pn):
        """Record lid -> phone mapping and merge anything stored under the lid into the phone identity."""
        lid, pn = norm_jid(lid), norm_jid(pn)
        if not lid or not pn or lid == pn or not lid.endswith("@lid") or not pn.endswith("@s.whatsapp.net"):
            return False
        if self.alias.get(lid) == pn:
            return False
        self.db.execute("insert or replace into aliases (jid, canon) values (?,?)", (lid, pn))
        self.alias[lid] = pn
        now = int(time.time())
        # people keyed by jid
        for t, col in (("messages", "sender"), ("reactions", "reactor"), ("group_members", "member"),
                       ("revocations", "by")):
            self.db.execute(f"update or ignore {t} set {col}=? where {col}=?", (pn, lid))
        old_c = self.db.execute("select * from contacts where jid=?", (lid,)).fetchone()
        if old_c:
            self.contact(pn, name=old_c["name"], push=old_c["push"], verified=old_c["verified"], weak=True)
        # chats
        lid_cid = self.chat_by_jid.get(lid)
        if lid_cid:
            pn_cid = self.chat_by_jid.get(pn)
            if pn_cid is None:
                # rename the chat itself to its phone identity
                new_id = self._new_id(pn)
                self.db.execute("update chats set id=?, jid=?, alt=?, updated=? where id=?", (new_id, pn, lid, now, lid_cid))
                self._move_chat_rows(lid_cid, new_id)
                del self.chat_by_jid[lid]
                self.chat_by_jid[pn] = new_id
                self.ids.add(new_id)
                self.ids.discard(lid_cid)
                pn_cid = new_id
            else:
                self._move_chat_rows(lid_cid, pn_cid)
                self.db.execute("update chats set merged_into=?, updated=? where id=?", (pn_cid, now, lid_cid))
                self.db.execute("update chats set alt=coalesce(alt, ?) where id=?", (lid, pn_cid))
            self.db.execute("insert or replace into chat_renames (old, new, ts) values (?,?,?)", (lid_cid, pn_cid, now))
            self.touched.update((lid_cid, pn_cid))
        else:
            pn_cid = self.chat_by_jid.get(pn)
            if pn_cid:
                self.db.execute("update chats set alt=coalesce(alt, ?) where id=?", (lid, pn_cid))
        self.db.execute("insert into changes (chat_id, ts) values ('*', ?)", (now,))
        return True

    def _move_chat_rows(self, old, new):
        self.db.execute("update or ignore messages set chat_id=? where chat_id=?", (new, old))
        self.db.execute("update or ignore group_members set chat_id=? where chat_id=?", (new, old))

    def contact(self, jid, name=None, push=None, verified=None, weak=False):
        """Upsert a contact; weak=True only fills empty fields."""
        jid = self.canon(jid)
        if not jid:
            return
        cur = self.db.execute("select name, push, verified from contacts where jid=?", (jid,)).fetchone()
        new = {"name": name or None, "push": push or None, "verified": verified or None}
        if cur is None:
            if any(new.values()):
                self.db.execute("insert into contacts (jid, name, push, verified, updated) values (?,?,?,?,?)",
                                (jid, new["name"], new["push"], new["verified"], int(time.time())))
                self.touched.add("*")
            return
        upd = {k: v for k, v in new.items() if v and cur[k] != v and not (weak and cur[k])}
        if upd:
            upd["updated"] = int(time.time())
            self.db.execute(f"update contacts set {','.join(k + '=?' for k in upd)} where jid=?", list(upd.values()) + [jid])
            self.touched.add("*")

    def member(self, chat_id, jid, admin=None, active=1):
        jid = self.canon(jid)
        cur = self.db.execute("select admin, active from group_members where chat_id=? and member=?", (chat_id, jid)).fetchone()
        now = int(time.time())
        if cur is None:
            self.db.execute("insert into group_members (chat_id, member, admin, active, updated) values (?,?,?,?,?)",
                            (chat_id, jid, int(bool(admin)), active, now))
            self.touched.add(chat_id)
        elif (admin is not None and cur["admin"] != int(bool(admin))) or cur["active"] != active:
            self.db.execute("update group_members set admin=coalesce(?, admin), active=?, updated=? where chat_id=? and member=?",
                            (None if admin is None else int(bool(admin)), active, now, chat_id, jid))
            self.touched.add(chat_id)

    # ---------- messages ----------
    def find(self, chat_id, key_id, from_me=None):
        if from_me is None:
            return self.db.execute("select * from messages where chat_id=? and key_id=? order by from_me limit 1",
                                   (chat_id, key_id)).fetchone()
        return self.db.execute("select * from messages where chat_id=? and key_id=? and from_me=?",
                               (chat_id, key_id, int(bool(from_me)))).fetchone()

    def upsert_message(self, chat_id, key_id, from_me, sender, ts, kind, text, data, source,
                       sort=0, text_ts=None, edited=False, revoked_at=None, revoked_by=None):
        """Insert or merge one message. kind == 'del' means "the source says this message is revoked".
        Returns the row id, or None when the message was purged (callers must tolerate None)."""
        from_me = int(bool(from_me))
        sender = self.canon(sender) if sender else None
        data = {k: v for k, v in (data or {}).items() if v is not None}
        now = int(time.time())
        row = self.find(chat_id, key_id, from_me)
        if row is None:
            if self.db.execute("select 1 from purged where key_id=? and from_me=?", (key_id, from_me)).fetchone():
                return None  # purged by the user: never re-insert (chat id may have changed since, so the chat is not part of the match)
            enrich = 0 if (kind in MEDIA_KINDS_ENRICH and data.get("m")) else 1
            cur = self.db.execute(
                """insert into messages (chat_id, key_id, from_me, sender, ts, sort, kind, text, data, revoked, edited,
                   text_ts, source, first_seen, updated, enrich) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (chat_id, key_id, from_me, sender, ts, sort, kind, text, json.dumps(data, ensure_ascii=False) if data else None,
                 int(kind == "del"), int(bool(edited)), text_ts or ts, source, now, now, enrich))
            mid = cur.lastrowid
            if kind == "del":
                self._revoke(mid, revoked_at or ts, revoked_by, source)
            self.touched.add(chat_id)
            return mid
        mid, changed = row["id"], False
        old = json.loads(row["data"]) if row["data"] else {}
        upd = {}
        if kind == "del":
            if not row["revoked"]:
                upd["revoked"] = 1
                self._revoke(mid, revoked_at or now, revoked_by, source)
        elif row["kind"] == "del" and not row["text"] and not old.get("m"):
            # placeholder (we only knew it was deleted) -> real content arrived (e.g. history sync)
            upd.update(kind=kind, text=text, data=json.dumps(data, ensure_ascii=False) if data else None,
                       sender=sender or row["sender"], ts=ts or row["ts"])
            if kind in MEDIA_KINDS_ENRICH and data.get("m"):
                upd["enrich"] = 0
        else:
            inc_ts = text_ts or ts
            if text is not None and (text or "") != (row["text"] or "") and kind == row["kind"]:
                if row["text_ts"] and inc_ts and inc_ts < row["text_ts"]:
                    # an older version (e.g. importing an older backup): keep it as history only
                    if text and not self.db.execute("select 1 from message_versions where msg_id=? and text=?",
                                                    (mid, text)).fetchone():
                        self.db.execute("insert into message_versions (msg_id, ts, text, source, recorded) values (?,?,?,?,?)",
                                        (mid, inc_ts, text, source, now))
                        upd["edited"] = 1
                else:
                    if row["text"]:
                        self.db.execute("insert into message_versions (msg_id, ts, text, source, recorded) values (?,?,?,?,?)",
                                        (mid, row["text_ts"] or row["ts"], row["text"], source, now))
                    upd.update(text=text, text_ts=inc_ts if text_ts else now, edited=1)
            elif edited and not row["edited"]:
                upd["edited"] = 1
            merged = dict(old)
            for k, v in data.items():
                if k in old and k not in ("m", "o", "lk", "sz", "n", "d", "pg"):
                    if k == "st" and isinstance(v, int) and isinstance(old[k], int):
                        v = max(v, old[k])   # delivery status only moves forward
                    else:
                        continue             # p/w/wh (enrichment), q, gs, …: first value wins
                if k == "gone" and old.get("m"):
                    continue
                if old.get(k) != v:
                    merged[k] = v
            if merged.get("m") and "gone" in merged:
                merged.pop("gone")
            if merged != old:
                upd["data"] = json.dumps(merged, ensure_ascii=False)
                if merged.get("m") != old.get("m") and kind in MEDIA_KINDS_ENRICH:
                    upd["enrich"] = 0
            if sender and not row["sender"]:
                upd["sender"] = sender
        if upd:
            upd["updated"] = now
            self.db.execute(f"update messages set {','.join(k + '=?' for k in upd)} where id=?", list(upd.values()) + [mid])
            self.touched.add(chat_id)
        return mid

    def _revoke(self, mid, deleted_at, by, source):
        self.db.execute("insert or ignore into revocations (msg_id, deleted_at, by, source, recorded) values (?,?,?,?,?)",
                        (mid, deleted_at, self.canon(by) if by and by != "me" else by, source, int(time.time())))

    def revoke(self, chat_id, key_id, from_me, deleted_at, by, source, sender=None, ts=None):
        """Mark an existing message revoked (or store a placeholder if we never saw it)."""
        row = self.find(chat_id, key_id, from_me)
        if row is None and from_me is not None:
            row = self.find(chat_id, key_id)
        if row is None:
            row = self.find_any(key_id, from_me)
        if row is None:
            return self.upsert_message(chat_id, key_id, from_me, sender, ts or deleted_at, "del", "", {}, source,
                                       revoked_at=deleted_at, revoked_by=by)
        if not row["revoked"]:
            self.db.execute("update messages set revoked=1, updated=? where id=?", (int(time.time()), row["id"]))
            self._revoke(row["id"], deleted_at, by, source)
            self.touched.add(chat_id)
        return row["id"]

    def find_any(self, key_id, from_me=None):
        """Locate a message by key id in any chat (edits/revokes may arrive with a lid/pn variant of the chat jid)."""
        rows = self.db.execute("select * from messages where key_id=? order by ts desc", (key_id,)).fetchall()
        if from_me is not None:
            rows = [r for r in rows if r["from_me"] == int(bool(from_me))] or rows
        return rows[0] if len(rows) >= 1 else None

    def edit(self, chat_id, key_id, from_me, new_text, ts, source):
        row = self.find(chat_id, key_id, from_me)
        if row is None and from_me is not None:
            row = self.find(chat_id, key_id)
        if row is None:
            row = self.find_any(key_id, from_me)
        if row is None:
            sys.stderr.write(f"edit: target not found chat={chat_id} key={key_id}\n")
        if row is None or new_text is None or (new_text or "") == (row["text"] or ""):
            return row["id"] if row else None
        now = int(time.time())
        if row["text"]:
            self.db.execute("insert into message_versions (msg_id, ts, text, source, recorded) values (?,?,?,?,?)",
                            (row["id"], row["text_ts"] or row["ts"], row["text"], source, now))
        self.db.execute("update messages set text=?, text_ts=?, edited=1, updated=? where id=?",
                        (new_text, ts or now, now, row["id"]))
        self.touched.add(chat_id)
        return row["id"]

    def react(self, msg_id, reactor, emoji, ts):
        reactor = "me" if reactor == "me" else self.canon(reactor)
        cur = self.db.execute("select emoji, ts from reactions where msg_id=? and reactor=?", (msg_id, reactor)).fetchone()
        if cur and (cur["ts"] or 0) > (ts or 0):
            return
        if cur is None or cur["emoji"] != (emoji or ""):
            self.db.execute("insert or replace into reactions (msg_id, reactor, emoji, ts) values (?,?,?,?)",
                            (msg_id, reactor, emoji or "", ts))
            chat = self.db.execute("select chat_id from messages where id=?", (msg_id,)).fetchone()
            if chat:
                self.touched.add(chat[0])

    def set_data(self, msg_id, **fields):
        row = self.db.execute("select chat_id, data from messages where id=?", (msg_id,)).fetchone()
        if not row:
            return
        d = json.loads(row["data"]) if row["data"] else {}
        d.update({k: v for k, v in fields.items() if v is not None})
        for k, v in fields.items():
            if v is None:
                d.pop(k, None)
        self.db.execute("update messages set data=?, updated=? where id=?",
                        (json.dumps(d, ensure_ascii=False), int(time.time()), msg_id))
        self.touched.add(row["chat_id"])

    def state(self, k, v=None):
        if v is None:
            r = self.db.execute("select v from sync_state where k=?", (k,)).fetchone()
            return r[0] if r else None
        self.db.execute("insert or replace into sync_state (k, v) values (?,?)", (k, str(v)))

    def commit(self):
        if self.touched:
            now = int(time.time())
            self.db.executemany("insert into changes (chat_id, ts) values (?,?)", [(c, now) for c in sorted(self.touched)])
            self.touched.clear()
        self.db.commit()


def purge_messages(db, ids, media=False):
    """Delete messages (by messages.id) with their versions/revocations/reactions/media rows, tombstone them in `purged`,
    and append the touched chats to `changes`. media=True also removes the media file (+ its thumbnail) when no remaining
    message/media row points at the same path. Returns {"removed": n, "files": n}. Commits."""
    ids = sorted({int(i) for i in ids})
    now = int(time.time())
    rows = []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        rows += db.execute(f"select id, chat_id, key_id, from_me, data from messages where id in ({','.join('?' * len(part))})", part).fetchall()
    paths = set()
    for r in rows:
        m = (json.loads(r["data"]) if r["data"] else {}).get("m")
        if isinstance(m, str) and m:
            paths.add(m)
    for i in range(0, len(rows), 500):
        part = rows[i:i + 500]
        q = ",".join("?" * len(part))
        pid = [r["id"] for r in part]
        for r in part:
            db.execute("insert or replace into purged (chat_id, key_id, from_me, ts) values (?,?,?,?)",
                       (r["chat_id"], r["key_id"], r["from_me"], now))
        for t, col in (("message_versions", "msg_id"), ("revocations", "msg_id"), ("reactions", "msg_id"),
                       ("media", "msg_id"), ("messages", "id")):
            db.execute(f"delete from {t} where {col} in ({q})", pid)
    for c in sorted({r["chat_id"] for r in rows}):
        db.execute("insert into changes (chat_id, ts) values (?,?)", (c, now))
    db.commit()
    files = 0
    if media and paths:
        used, plist = set(), sorted(paths)
        for i in range(0, len(plist), 500):
            part = plist[i:i + 500]
            q = ",".join("?" * len(part))
            used.update(r[0] for r in db.execute(f"select json_extract(data, '$.m') from messages where json_extract(data, '$.m') in ({q})", part))
            used.update(r[0] for r in db.execute(f"select path from media where path in ({q})", part))
        store = os.path.realpath(waconf.STORE)
        for m in paths - used:
            f = os.path.realpath(os.path.join(store, m))
            if not f.startswith(store + os.sep):
                continue  # never touch anything outside the store
            for victim in (f, os.path.join(waconf.THUMBS, hashlib.sha1(m.encode("utf-8")).hexdigest() + ".jpg")):
                try:
                    os.remove(victim)
                    files += victim == f
                except OSError:
                    pass
    return {"removed": len(rows), "files": files}
