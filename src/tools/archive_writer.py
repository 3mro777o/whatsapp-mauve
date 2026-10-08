#!/usr/bin/env python3
"""archive.db writer for wa-sync. Reads JSON lines (events) on stdin, writes archive.db via archive_db.
Writes JSON lines on stdout for wa-sync: {"job": {...}} (on-demand media downloads requested by the viewer).

Events:
  {"op":"alias","lid","pn"}                         {"op":"contact","jid","name"?,"push"?,"verified"?}
  {"op":"chat","jid","subject"?,"type"?,"parent_jid"?,"archived"?,"created"?,"meta"?}
  {"op":"members","chat_jid","members":[{"jid","admin"}],"full":true}
  {"op":"member","chat_jid","jid","active","admin"?}
  {"op":"msg","chat_jid","key_id","from_me","sender","ts","kind","text","data","source","stub"?,"quote"?,"media"?}
  {"op":"edit","chat_jid","key_id","from_me","text","ts","source"}
  {"op":"revoke","chat_jid","key_id","from_me","ts","by","source"}
  {"op":"dmark","chat_jid","key_id","from_me"}   (deleted for me: data.dm=1, content kept)
  {"op":"react","chat_jid","key_id","from_me","reactor","emoji","ts"}
  {"op":"media","chat_jid","key_id","from_me","state","path"?,"progress"?,"error"?,"size"?}
  {"op":"job","id","state","result"?}               {"op":"avatar","jid","b64"}
  {"op":"state","k","v"}
"""
import base64, json, os, select, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import waconf
from archive_db import Archive

STUB = {  # Baileys WAMessageStubType -> Arabic template ({a}=actor, {p}=participants, {t}=param)
    "GROUP_CREATE": "{a} أنشأ المجموعة «{t}»", "GROUP_CHANGE_SUBJECT": "{a} غيّر اسم المجموعة إلى «{t}»",
    "GROUP_CHANGE_ICON": "{a} غيّر صورة المجموعة", "GROUP_CHANGE_DESCRIPTION": "{a} غيّر وصف المجموعة",
    "GROUP_PARTICIPANT_ADD": "{a} أضاف {p}", "GROUP_PARTICIPANT_REMOVE": "{a} أزال {p}",
    "GROUP_PARTICIPANT_PROMOTE": "{p} صار مشرفاً", "GROUP_PARTICIPANT_DEMOTE": "{p} ما عاد مشرفاً",
    "GROUP_PARTICIPANT_INVITE": "{p} انضم عبر رابط الدعوة", "GROUP_PARTICIPANT_LEAVE": "{p} غادر",
    "GROUP_PARTICIPANT_CHANGE_NUMBER": "{a} غيّر رقمه", "GROUP_CHANGE_RESTRICT": "{a} غيّر إعدادات المجموعة",
    "GROUP_CHANGE_ANNOUNCE": "{a} غيّر إعدادات المجموعة", "GROUP_PARTICIPANT_ACCEPT": "{p} انضم",
    "GROUP_MEMBER_ADD_MODE": "{a} غيّر إعدادات المجموعة", "CHANGE_EPHEMERAL_SETTING": "{a} غيّر إعدادات الرسائل المؤقتة",
    "COMMUNITY_LINK_SUB_GROUP": "{a} ربط مجموعة بالمجتمع", "COMMUNITY_LINK_PARENT_GROUP": "{a} ربط المجموعة بمجتمع",
    "COMMUNITY_CREATE": "{a} أنشأ المجتمع «{t}»",
}

out_lock = threading.Lock()


def emit(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class Writer:
    def __init__(self):
        self.ar = Archive()
        self.enrich_due = 0
        self.enrich_proc = None
        self.dirty = False

    def cid(self, jid, **f):
        return self.ar.ensure_chat(jid, **f)

    def name(self, jid):
        if not jid:
            return None
        if jid == "me":
            return "أنت"
        c = self.ar.db.execute("select name, push, verified from contacts where jid=?", (self.ar.canon(jid),)).fetchone()
        if c and (c[0] or c[1] or c[2]):
            return c[0] or c[1] or c[2]
        u = jid.split("@")[0]
        return "+" + u if u.isdigit() else u

    def handle(self, e):
        op = e.get("op")
        ar = self.ar
        if op == "alias":
            ar.set_alias(e["lid"], e["pn"])
        elif op == "contact":
            ar.contact(e["jid"], name=e.get("name"), push=e.get("push"), verified=e.get("verified"))
        elif op == "chat":
            f = {k: e.get(k) for k in ("subject", "type", "parent_jid", "archived", "created")}
            if f.get("parent_jid"):
                f["parent_jid"] = ar.canon(f["parent_jid"])
            ar.ensure_chat(e["jid"], meta=e.get("meta"), **f)
        elif op == "members":
            cid = self.cid(e["chat_jid"])
            seen = set()
            for m in e.get("members", []):
                ar.member(cid, m["jid"], admin=m.get("admin"), active=1)
                seen.add(ar.canon(m["jid"]))
            if e.get("full"):
                for r in ar.db.execute("select member from group_members where chat_id=? and active=1", (cid,)).fetchall():
                    if r[0] not in seen:
                        ar.member(cid, r[0], active=0)
        elif op == "member":
            ar.member(self.cid(e["chat_jid"]), e["jid"], admin=e.get("admin"), active=int(e.get("active", 1)))
        elif op == "unread":  # {"jid", "n"?, "inc"?, "up"?}: up = count reported by WhatsApp (absolute), "mark" = marked unread, "upto" = [key_ids] read on another device
            cid = ar.chat_by_jid.get(ar.canon(e["jid"]))
            if cid is None and e.get("n"):
                cid = self.cid(e["jid"])
            if cid is not None:
                if e.get("upto"):  # read elsewhere up to these incoming messages: unread = incoming newer than the newest of them
                    ids = [str(i) for i in e["upto"]][:200]
                    t = ar.db.execute(f"select max(ts) from messages where chat_id=? and from_me=0 and key_id in ({','.join('?' * len(ids))})", [cid, *ids]).fetchone()[0]
                    if t is not None:
                        n = ar.db.execute("select count(*) from messages where chat_id=? and from_me=0 and ts>? and kind is not 'sys'", (cid, t)).fetchone()[0]
                        ar.set_unread(cid, min(n, ar.db.execute("select coalesce(unread,0) from chats where id=?", (cid,)).fetchone()[0]))
                elif e.get("mark"):
                    if not (ar.db.execute("select unread from chats where id=?", (cid,)).fetchone()[0] or 0):
                        ar.set_unread(cid, 1)
                else:
                    ar.set_unread(cid, e.get("n"), e.get("inc", 0), clamp=bool(e.get("up")))
        elif op == "msg":
            self.msg(e)
        elif op == "edit":
            ar.edit(self.cid(e["chat_jid"]), e["key_id"], e.get("from_me"), e.get("text"), e.get("ts"), e.get("source", "live"))
        elif op == "revoke":
            cid = self.cid(e["chat_jid"])
            ar.revoke(cid, e["key_id"], e.get("from_me"), e.get("ts") or int(time.time()), e.get("by"),
                      e.get("source", "live"), sender=e.get("sender"), ts=e.get("msg_ts"))
        elif op == "react":
            cid = self.cid(e["chat_jid"])
            row = ar.find(cid, e["key_id"], e.get("from_me"))
            if row is None:
                row = ar.find(cid, e["key_id"])
            if row is not None:
                ar.react(row["id"], e["reactor"], e.get("emoji") or "", e.get("ts"))
        elif op == "dmark":  # "deleted for me" marker; the archive keeps the content
            cid = self.cid(e["chat_jid"])
            row = ar.find(cid, e["key_id"], e.get("from_me")) or ar.find(cid, e["key_id"])
            if row is not None:
                ar.set_data(row["id"], dm=1)
        elif op == "media":
            self.media(e)
        elif op == "job":
            ar.db.execute("update jobs set state=?, result=?, updated=? where id=?",
                          (e["state"], json.dumps(e.get("result")) if e.get("result") is not None else None,
                           int(time.time()), e["id"]))
        elif op == "avatar":
            import avatars_lib as av
            try:
                j = e["jid"].split("@")[0]
                if e.get("remove"):  # the contact/group removed its picture: keep the old one in history
                    av.archive_current(j)
                else:
                    av.save_avatar_key(j, base64.b64decode(e["b64"]))
            except Exception as ex:
                print("avatar error:", ex, file=sys.stderr)
        elif op == "state":
            ar.state(e["k"], e["v"])
        self.dirty = True

    def msg(self, e):
        ar = self.ar
        cid = self.cid(e["chat_jid"])
        data = dict(e.get("data") or {})
        kind, text = e.get("kind"), e.get("text")
        st = e.get("stub")
        if st:
            tpl = STUB.get(st["type"])
            if not tpl:
                return
            actor = "أنت" if e.get("from_me") else (self.name(st.get("actor") or e.get("sender")) or "")
            ps = "، ".join(filter(None, (self.name(p) if "@" in str(p) else str(p) for p in st.get("params") or [])))
            text = tpl.format(a=actor, p=ps, t=(st.get("params") or [""])[0] if st.get("params") else "")
            kind = "sys"
        q = e.get("quote")
        if q:
            data["q"] = [q["id"], "أنت" if q.get("from_me") else (self.name(q.get("jid")) or ""), q.get("text") or "رسالة"]
        existed = ar.find(cid, e["key_id"], e.get("from_me")) is not None
        mid = ar.upsert_message(cid, e["key_id"], e.get("from_me"), e.get("sender"), int(e["ts"]), kind, text, data,
                                e.get("source", "live"), sort=e.get("sort", 0), edited=e.get("edited", False))
        if mid and "unread" in e:  # live message (wa-sync): mine = I replied/read on the phone, theirs (new) = +1
            if not e["unread"]:
                ar.set_unread(cid, 0)
            elif not existed:
                ar.set_unread(cid, inc=1)
        md = e.get("media")
        if md and mid:  # mid None = purged by the user
            self.media_row(mid, md, data)

    def media_row(self, mid, md, data):
        now = int(time.time())
        cur = self.ar.db.execute("select state from media where msg_id=?", (mid,)).fetchone()
        info = json.dumps(md.get("info")) if md.get("info") else None
        if cur is None:
            self.ar.db.execute("""insert into media (msg_id, path, size, mime, downloaded, state, progress, name, kind, info,
                                  error, created, updated) values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (mid, md.get("path"), md.get("size"), md.get("mime"), int(md.get("state") == "done"),
                                md.get("state"), None, md.get("name"), md.get("kind"), info, md.get("error"), now, now))
        else:
            # keep a finished download; otherwise update keys/state
            if cur["state"] == "done":
                if info:
                    self.ar.db.execute("update media set info=coalesce(info, ?) where msg_id=?", (info, mid))
                return
            if cur["state"] in ("queued", "downloading", "skipped") and md.get("state") in ("pending", "remote"):
                md = {**md, "state": cur["state"]}
            self.ar.db.execute("""update media set path=coalesce(?, path), size=coalesce(?, size), mime=coalesce(?, mime),
                                  downloaded=?, state=?, name=coalesce(?, name), kind=coalesce(?, kind), info=coalesce(?, info),
                                  error=?, updated=? where msg_id=?""",
                               (md.get("path"), md.get("size"), md.get("mime"), int(md.get("state") == "done"),
                                md.get("state"), md.get("name"), md.get("kind"), info, md.get("error"), now, mid))
        if md.get("state") == "done" and md.get("path"):
            self.ar.set_data(mid, m=md["path"], gone=None)
            self.ar.db.execute("update messages set enrich=0 where id=? and kind in ('vid','ptt','doc')", (mid,))
            self.enrich_due = time.time() + 20

    def media(self, e):
        cid = self.cid(e["chat_jid"])
        row = self.ar.find(cid, e["key_id"], e.get("from_me")) or self.ar.find(cid, e["key_id"])
        if row is None:
            return
        if e.get("state") == "downloading" and e.get("progress") is not None:
            self.ar.db.execute("update media set state='downloading', progress=?, updated=? where msg_id=?",
                               (e["progress"], int(time.time()), row["id"]))
            self.ar.touched.add(cid)
            return
        self.media_row(row["id"], {k: e.get(k) for k in ("state", "path", "size", "error", "info", "mime", "name", "kind")},
                       {})
        if e.get("state") == "done":
            self.ar.db.execute("update media set progress=1 where msg_id=?", (row["id"],))
        self.ar.touched.add(cid)

    def poll_jobs(self):
        rows = self.ar.db.execute("select * from jobs where state='queued' order by id").fetchall()
        for j in rows:
            self.ar.db.execute("update jobs set state='running', updated=? where id=?", (int(time.time()), j["id"]))
            if j["kind"] == "media":
                m = self.ar.db.execute("""select m.chat_id, m.key_id, m.from_me, m.kind, c.jid, md.info, md.size, md.name, md.kind mk
                                          from messages m join chats c on c.id=m.chat_id join media md on md.msg_id=m.id
                                          where m.id=?""", (int(j["arg"]),)).fetchone()
                if m and m["info"]:
                    emit({"job": {"id": j["id"], "kind": "media", "chat_jid": m["jid"], "key_id": m["key_id"],
                                  "from_me": m["from_me"], "msg_kind": m["kind"], "info": json.loads(m["info"]),
                                  "size": m["size"], "name": m["name"]}})
                else:
                    self.ar.db.execute("update jobs set state='error', result='\"unavailable\"' where id=?", (j["id"],))
        if rows:
            self.ar.commit()

    def maybe_enrich(self):
        if self.enrich_due and time.time() > self.enrich_due and (self.enrich_proc is None or self.enrich_proc.poll() is not None):
            self.enrich_due = 0
            self.enrich_proc = subprocess.Popen([sys.executable, os.path.join(waconf.TOOLS, "enrich.py")],
                                                stdout=subprocess.DEVNULL, stderr=sys.stderr)

    def run(self):
        fd = sys.stdin.fileno()
        buf = b""
        last_jobs = 0
        while True:
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        self.handle(json.loads(line))
                    except Exception as ex:
                        print("writer error:", type(ex).__name__, ex, line[:200], file=sys.stderr)
            if self.dirty:
                self.ar.commit()
                self.dirty = False
            if time.time() - last_jobs > 2:
                last_jobs = time.time()
                try:
                    self.poll_jobs()
                except Exception as ex:
                    print("jobs error:", ex, file=sys.stderr)
            self.maybe_enrich()
        self.ar.commit()


if __name__ == "__main__":
    Writer().run()
