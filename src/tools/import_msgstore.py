#!/usr/bin/env python3
"""Import a WhatsApp Android backup (msgstore.db) into archive.db — additive, never deletes.

  import_msgstore.py                 import ./msgstore.db (already decrypted)
  import_msgstore.py FILE.db         import a decrypted msgstore
  import_msgstore.py X.db.crypt15    decrypt + import a specific (e.g. older) backup
  import_msgstore.py --latest        decrypt the newest $WA_BACKUPS/msgstore*.db.crypt15 with $WA_KEY_FILE and import it
  import_msgstore.py --mirror-only   only hardlink the phone Media folder into the store
Options: --no-mirror (skip media mirroring), --force (re-import even if this file was imported before)
"""
import glob, hashlib, json, os, shutil, sqlite3, subprocess, sys, tempfile, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import waconf
from archive_db import Archive, chat_type_for, norm_jid

SYSTEM = {
    1: "{a} غيّر اسم المجموعة إلى «{t}»", 4: "{a} انضم", 5: "{a} غادر",
    6: "{a} غيّر صورة المجموعة", 10: "{a} غيّر رقمه", 11: "{a} أنشأ المجموعة «{t}»",
    12: "{a} أضاف {p}", 13: "{a} أزال {p}", 14: "{a} أزال {p}", 15: "{a} صار مشرفاً",
    18: "تغيّر رمز الأمان لـ {a}", 19: "🔒 الرسائل مشفّرة تماماً بين الطرفين",
    20: "{a} انضم عبر رابط الدعوة", 27: "{a} غيّر وصف المجموعة", 28: "{a} غيّر رقمه",
    58: "{a} غيّر إعدادات الرسائل المؤقتة", 67: "🔒 الرسائل والمكالمات مشفّرة تماماً بين الطرفين",
    69: "🔒 تغيّر رمز الأمان", 129: "{a} غيّر إعدادات المجموعة",
}
QUOTE_LABEL = {1: "📷 صورة", 3: "🎥 فيديو", 2: "🎤 رسالة صوتية", 9: "📄 مستند", 20: "ملصق"}


def log(*a):
    print(*a, flush=True)


# ---------------- media store ----------------
def store_path(rel):
    return os.path.join(waconf.STORE, rel)


def link_into_store(src, rel):
    """Hardlink (or reflink/copy) src into the store at rel. Returns True if the store has the file."""
    dst = store_path(rel)
    if os.path.exists(dst):
        return True
    if not os.path.isfile(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        try:
            subprocess.run(["cp", "--reflink=auto", "--preserve=timestamps", src, dst], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            shutil.copy2(src, dst)
    return True


def phone_src(rel):
    """msgstore paths look like 'Media/WhatsApp Images/x.jpg' (relative to the phone's WhatsApp folder)."""
    parts = rel.split("/", 1)
    if parts[0] == "Media" and len(parts) == 2:
        return os.path.join(waconf.PHONE_MEDIA, parts[1])
    return os.path.join(os.path.dirname(waconf.PHONE_MEDIA), rel)


def dst_rel(rel):
    """'Media/x' (msgstore naming) -> the store path for this layout (WA_MEDIA_DIR, default Media)."""
    return waconf.MEDIA_DIR + rel[5:] if rel.startswith("Media/") else rel


# phone Media junk that is never mirrored (kept: .Statuses, WhatsApp AI Media, stickers, everything else)
JUNK_DIRS = {".Links", ".Wamo", ".wamocache", "WhatsApp Bug Report Attachments"}
def junk_dir(d): return d in JUNK_DIRS or d.startswith(".udDH")


def mirror_media():
    """Hardlink (or copy) every file of the phone Media folder into the store (so later deletions lose nothing)."""
    if not os.path.isdir(waconf.PHONE_MEDIA):
        log("media mirror: no phone Media folder at", waconf.PHONE_MEDIA)
        return
    t0, n, new = time.time(), 0, 0
    for dirpath, dirs, files in os.walk(waconf.PHONE_MEDIA):
        dirs[:] = [d for d in dirs if not junk_dir(d)]
        for fn in files:
            if fn == ".nomedia":
                continue
            src = os.path.join(dirpath, fn)
            rel = waconf.MEDIA_DIR + "/" + os.path.relpath(src, waconf.PHONE_MEDIA)
            n += 1
            if not os.path.exists(store_path(rel)):
                link_into_store(src, rel)
                new += 1
    log(f"media mirror: {n} files, {new} newly linked ({time.time() - t0:.1f}s)")


# ---------------- decryption ----------------
def decrypt_latest():
    files = sorted(glob.glob(os.path.join(waconf.BACKUPS, "msgstore*.db.crypt15")), key=os.path.getmtime)
    if not files:
        sys.exit(f"no msgstore*.db.crypt15 in {waconf.BACKUPS}")
    return decrypt_file(files[-1])


def decrypt_file(src):
    try:
        key = open(waconf.KEY_FILE).read().strip().replace(" ", "")
    except OSError:
        sys.exit(f"no backup key at {waconf.KEY_FILE}: put the 64-digit end-to-end backup key there (wa key <name>)")
    if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
        sys.exit("the backup key must be exactly 64 hex digits (0-9, a-f)")
    out = os.path.join(waconf.STATE, "msgstore.decrypted.db")
    os.makedirs(waconf.STATE, exist_ok=True)
    py = os.path.join(waconf.HOME, ".venv", "bin", "python")
    code = ("import sys\nfrom Whatsapp_Chat_Exporter.android_crypt import decrypt_backup\n"
            "from Whatsapp_Chat_Exporter.utility import Crypt\n"
            "db=open(sys.argv[1],'rb').read()\n"
            "rc=decrypt_backup(db, bytes.fromhex(sys.stdin.read().strip().replace(' ','')), sys.argv[2], Crypt.CRYPT15)\n"
            "sys.exit(rc or 0)\n")
    log("decrypting", os.path.basename(src))
    r = subprocess.run([py if os.path.exists(py) else sys.executable, "-c", code, src, out], input=key, text=True, stderr=subprocess.PIPE)
    if r.returncode != 0 or not os.path.exists(out):
        why = (r.stderr.strip().splitlines() or ["?"])[-1]
        sys.exit(f"decryption failed ({why}): wrong key or not a crypt15 backup?")
    return out, src


def sha_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------- import ----------------
def load_names():
    """Address-book names: wtsexporter output (enriched from contacts.vcf) + wa.db."""
    names = {}
    p = os.path.join(waconf.HOME, "archive", "chats.json")
    if os.path.exists(p):
        for jid, c in json.load(open(p, encoding="utf-8")).items():
            if c.get("name") and c["name"] != jid.split("@")[0]:
                names[norm_jid(jid)] = c["name"]
    wa = os.path.join(waconf.HOME, "wa.db")
    if os.path.exists(wa):
        try:
            w = sqlite3.connect(f"file:{wa}?mode=ro", uri=True)
            for jid, dn in w.execute("select jid, display_name from wa_contacts where display_name is not null and display_name != ''"):
                names.setdefault(norm_jid(jid), dn)
        except sqlite3.Error:
            pass
    return names


def import_db(path, ar, names):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    has = lambda t: db.execute("select 1 from sqlite_master where name=?", (t,)).fetchone() is not None
    jids = {r["_id"]: norm_jid(r["raw_string"]) for r in db.execute("select _id, raw_string from jid")}
    t0 = time.time()

    # lid <-> phone first, so everything lands on the phone identity
    if has("jid_map"):
        for r in db.execute("select lid_row_id, jid_row_id from jid_map"):
            if r[0] in jids and r[1] in jids:
                ar.set_alias(jids[r[0]], jids[r[1]])
    for jid, n in names.items():
        ar.contact(jid, name=n)

    def name(row):
        j = jids.get(row)
        if not j:
            return None
        j = ar.canon(j)
        u = j.split("@")[0]
        return names.get(j) or names.get(norm_jid(jids.get(row))) or ("+" + u if u.isdigit() else u)

    def lookup(table, key="message_row_id"):
        return {r[key]: r for r in db.execute(f"select * from {table}")} if has(table) else {}

    media, quoted, system = lookup("message_media"), lookup("message_quoted"), lookup("message_system")
    location, edited, text_extra = lookup("message_location"), lookup("message_edit_info"), lookup("message_text")
    revoked = lookup("message_revoked")
    calls = {r["message_row_id"]: r for r in db.execute(
        "select mc.message_row_id, c.* from message_call_log mc join call_log c on c._id = mc.call_log_row_id")}
    participants = defaultdict(list)
    for r in db.execute("select * from message_system_chat_participant"):
        participants[r["message_row_id"]].append(r["user_jid_row_id"])
    polls = defaultdict(list)
    if has("message_poll_option"):
        for r in db.execute("select message_row_id, option_name, vote_total from message_poll_option order by _id"):
            polls[r[0]].append([r[1], r[2] or 0])
    reactions = defaultdict(list)
    for r in db.execute("""select a.parent_message_row_id p, a.from_me, a.sender_jid_row_id s, a.timestamp ts, r.reaction
                           from message_add_on a join message_add_on_reaction r on r.message_add_on_row_id = a._id"""):
        reactions[r["p"]].append(("me" if r["from_me"] else jids.get(r["s"]), r["reaction"] or "", int((r["ts"] or 0) / 1000)))
    status_posts = {r[0] for r in db.execute("select message_row_id from status_message_info")} if has("status_message_info") else set()

    # community structure (see build notes): node type 1 = parent, 2 = subgroup
    chat_rows = {r["_id"]: r for r in db.execute(
        "select c.*, j.raw_string jid from chat c join jid j on j._id = c.jid_row_id")}
    chat_of_jidrow = {r["jid_row_id"]: r["_id"] for r in chat_rows.values()}
    parent_of = {}
    if has("message_system_with_group_nodes"):
        nodes = defaultdict(list)
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
            if host != par and host in chat_rows and chat_rows[host]["group_type"] in (2, 3, 6):
                parent_of.setdefault(host, par)
    newsletter_names = {}
    if has("newsletter"):
        newsletter_names = {r[0]: r[1] for r in db.execute("select chat_row_id, name from newsletter") if r[1]}

    stats = defaultdict(int)
    store_has = {}
    idmap = {}

    def media_path(rel):
        if not rel:
            return None
        d = dst_rel(rel)
        if d not in store_has:
            store_has[d] = link_into_store(phone_src(rel), d)
        return d if store_has[d] else None

    for crow in chat_rows.values():
        jid = norm_jid(crow["jid"])
        if jid.split("@")[-1] in ("temp",):
            continue
        gt = crow["group_type"] or 0
        ctype = chat_type_for(jid, gt)
        par = parent_of.get(crow["_id"])
        cid = ar.ensure_chat(jid, subject=crow["subject"] or newsletter_names.get(crow["_id"]), type=ctype,
                             archived=crow["archived"] or 0, group_type=gt, msgstore_id=crow["_id"],
                             created=int((crow["created_timestamp"] or 0) / 1000) or None,
                             parent_jid=norm_jid(chat_rows[par]["jid"]) if par in chat_rows else None)
        idmap[str(crow["_id"])] = cid
        canon_jid = ar.canon(jid)
        cname = crow["subject"] or names.get(jid) or ("+" + jid.split("@")[0])
        is_group = jid.endswith("@g.us")
        for m in db.execute("""select * from message where chat_row_id = ? and timestamp > 0
                               order by sort_id, timestamp""", (crow["_id"],)):
            mid, t = m["_id"], m["message_type"]
            ts = int(m["timestamp"] / 1000)
            o, kind, text = {}, None, m["text_data"]
            if m["from_me"]:
                o["st"] = m["status"]
            sender = None if m["from_me"] else (jids.get(m["sender_jid_row_id"]) if m["sender_jid_row_id"] else
                                                (canon_jid if not is_group else None))
            if t == 7:
                s = system.get(mid)
                tpl = SYSTEM.get(s["action_type"] if s else None)
                if not tpl:
                    continue
                actor = "أنت" if m["from_me"] else (name(m["sender_jid_row_id"]) or cname)
                ps = "، ".join(filter(None, (name(p) for p in participants.get(mid, [])))) or ""
                kind, text, o = "sys", tpl.format(a=actor, p=ps, t=text or ""), {}
            elif t == 90 or mid in calls:
                c = calls.get(mid)
                if not c:
                    continue
                kind = "call"
                o.update(v=c["video_call"], d=c["duration"])
                text = ("مكالمة فيديو" if c["video_call"] else "مكالمة صوتية") if c["duration"] else \
                    ("مكالمة فائتة" if not c["from_me"] else "لم يتم الرد")
            elif t == 15:
                kind, text = "del", ""
            elif t == 99 or mid in polls:
                kind, text = "poll", text or ""
                o["o"] = polls.get(mid, [])
            elif mid in location:
                l = location[mid]
                kind, text = "loc", l["place_name"] or ("موقع مباشر" if t == 16 else "موقع")
                o["ll"] = [l["latitude"], l["longitude"]]
            elif mid in media:
                md = media[mid]
                path, mime = md["file_path"], md["mime_type"] or ""
                kind = ("sticker" if t == 20 else "img" if mime.startswith("image") or t in (1, 42)
                        else "vid" if mime.startswith("video") or t in (3, 13, 43) else
                        "ptt" if t in (2, 81, 82) or mime.startswith("audio") else "doc")
                if t == 13:
                    o["gif"] = 1
                if kind == "vid" and "WhatsApp Video Notes" in (path or ""):
                    o["ptv"] = 1  # round video note (sent/received as a circle)
                mp = media_path(path)
                if mp:
                    o["m"] = mp
                else:
                    o["gone"] = 1  # no file and no usable media key in a backup
                if md["width"] and md["height"]:
                    o["wh"] = [md["width"], md["height"]]
                if kind == "doc":
                    o["n"] = md["media_name"] or (os.path.basename(path) if path else "مستند")
                    if md["page_count"]:
                        o["pg"] = md["page_count"]
                if md["file_size"]:
                    o["sz"] = md["file_size"]
                if md["media_duration"]:
                    o["d"] = md["media_duration"]
                text = text or md["media_caption"]
            elif text is None and t not in (0,):
                if t in (64, 42, 43, 81, 82):
                    kind, text = "sys2", "📷 رسالة للعرض مرة واحدة"
                elif t == 4:
                    text = "👤 جهة اتصال"
                else:
                    continue
            else:
                text = text or ""
                te = text_extra.get(mid)
                if te and te["url"]:
                    o["lk"] = [te["page_title"] or "", te["description"] or "", te["url"]]
            q = quoted.get(mid)
            if q:
                o["q"] = [q["key_id"], "أنت" if q["from_me"] else (name(q["sender_jid_row_id"]) or cname),
                          q["text_data"] or QUOTE_LABEL.get(q["message_type"], "رسالة")]
            if mid in status_posts:
                o["gs"] = 1
            e = edited.get(mid)
            rv = revoked.get(mid)
            msg_id = ar.upsert_message(
                cid, m["key_id"], m["from_me"], sender, ts, kind, text, o, "backup", sort=m["sort_id"] or 0,
                text_ts=int(e["edited_timestamp"] / 1000) if e and e["edited_timestamp"] else None, edited=bool(e),
                revoked_at=int(rv["revoke_timestamp"] / 1000) if rv and rv["revoke_timestamp"] else None,
                revoked_by=(jids.get(rv["admin_jid_row_id"]) if rv and rv["admin_jid_row_id"] else
                            ("me" if m["from_me"] else sender)) if t == 15 else None)
            stats["messages"] += 1
            for reactor, emoji, rts in reactions.get(mid, ()):
                if reactor and msg_id:  # msg_id None = purged by the user
                    ar.react(msg_id, reactor, emoji, rts)
            if stats["messages"] % 20000 == 0:
                ar.commit()
                log(f"  {stats['messages']} messages ({time.time() - t0:.0f}s)")
        stats["chats"] += 1

    # group members (current) from the backup
    if has("group_participant_user"):
        for r in db.execute("select group_jid_row_id g, user_jid_row_id u, rank from group_participant_user"):
            g, u = jids.get(r["g"]), jids.get(r["u"])
            if g and u and g.endswith("@g.us") and "@" in u and u.split("@")[1] in ("s.whatsapp.net", "lid"):
                cid = ar.chat_by_jid.get(ar.canon(g))
                if cid:
                    ar.member(cid, u, admin=(r["rank"] or 0) > 0)
    ar.state("msgstore_idmap", json.dumps(idmap))
    ar.commit()
    stats["seconds"] = round(time.time() - t0, 1)
    return dict(stats)


# ---------------- message secrets for wa-sync (edit decryption) ----------------
def export_secrets(path):
    """Append missing message secrets (msgstore table message_secret) to $WA_STATE/msg-secrets.jsonl
    and tell a running wa-sync to reload them (SIGHUP)."""
    import base64
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    if not db.execute("select 1 from sqlite_master where name='message_secret'").fetchone():
        return 0
    out = os.path.join(waconf.STATE, "msg-secrets.jsonl")
    have = set()
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            for line in f:
                try:
                    have.add(json.loads(line)["k"])
                except (ValueError, KeyError):
                    pass
    me = os.environ.get("WA_ME") or waconf.CONF.get("WA_ME")
    if not me:
        try:
            me = json.load(open(os.path.join(waconf.STATE, "status.json"))).get("me")
        except (OSError, ValueError):
            me = None
    me = norm_jid(me) if me else None
    rows = db.execute("""select m.key_id, m.from_me, ms.message_secret, sj.raw_string sender, cj.raw_string chat
                         from message_secret ms join message m on m._id = ms.message_row_id
                         join chat c on c._id = m.chat_row_id join jid cj on cj._id = c.jid_row_id
                         left join jid sj on sj._id = m.sender_jid_row_id""")
    new = []
    for key, from_me, secret, sender, chat in rows:
        if not key or not secret or key in have:
            continue
        author = me if from_me else norm_jid(sender or chat)
        if not author:
            continue
        new.append(json.dumps({"k": key, "s": base64.b64encode(secret).decode(), "a": author}))
        have.add(key)
    if new:
        fd = os.open(out, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("\n".join(new) + "\n")
        try:
            pid = json.load(open(os.path.join(waconf.STATE, "status.json"))).get("pid")
            if pid:
                os.kill(int(pid), 1)  # SIGHUP -> wa-sync reloads secrets
        except (OSError, ValueError, TypeError):
            pass
    log(f"message secrets: {len(new)} new" + ("" if me else " (own messages skipped: WA_ME unknown)"))
    return len(new)


# ---------------- one-time migration of user data keyed by old chat ids ----------------
def migrate_user_data(ar):
    if ar.state("migrated_user_v1"):
        return
    m = json.loads(ar.state("msgstore_idmap") or "{}")
    for fn in ("bookmarks.json", "bookmarks.json.bak"):
        p = os.path.join(waconf.USER, fn)
        if not os.path.exists(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except ValueError:
            continue
        n = 0
        for b in data if isinstance(data, list) else []:
            if isinstance(b, dict) and str(b.get("chat")) in m:
                b["chat"] = m[str(b["chat"])]
                n += 1
        shutil.copy2(p, p + ".pre-migration")
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)
        log(f"migrated {n} bookmarks in {fn}")
    # lid-keyed avatars / people entries -> phone
    alias = {k.split("@")[0]: v.split("@")[0] for k, v in ar.alias.items()}
    av = os.path.join(waconf.USER, "avatars")
    if os.path.isdir(av):
        for f in os.listdir(av):
            stem, ext = os.path.splitext(f)
            if stem in alias and not any(x.startswith(alias[stem] + ".") for x in os.listdir(av)):
                os.rename(os.path.join(av, f), os.path.join(av, alias[stem] + ext))
    pp = os.path.join(waconf.USER, "people.json")
    if os.path.exists(pp):
        d = json.load(open(pp, encoding="utf-8"))
        nn = d.get("nicknames", {})
        d["nicknames"] = {alias.get(k, k): v for k, v in nn.items()}
        for g in d.get("groups", []):
            g["members"] = list(dict.fromkeys(alias.get(x, x) for x in g.get("members", [])))
        shutil.copy2(pp, pp + ".pre-migration")
        with open(pp + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(pp + ".tmp", pp)
    with open(os.path.join(waconf.STATE, "chat-id-map.json"), "w") as f:
        json.dump(m, f)
    ar.state("migrated_user_v1", int(time.time()))
    ar.commit()


def main():
    args = sys.argv[1:]
    os.makedirs(waconf.STATE, exist_ok=True)
    if "--mirror-only" in args:
        return mirror_media()
    if "--no-mirror" not in args:
        mirror_media()
    files = [a for a in args if not a.startswith("--")]
    if "--latest" in args:
        path, origin = decrypt_latest()
    elif files and files[0].endswith(".crypt15"):
        path, origin = decrypt_file(files[0])
    else:
        path = files[0] if files else os.path.join(waconf.HOME, "msgstore.db")
        origin = path
    try:
        ar = Archive()
        sha = sha_of(path)
        if "--force" not in args and ar.db.execute("select 1 from imports where sha=?", (sha,)).fetchone():
            log("already imported:", os.path.basename(origin))
        else:
            log("importing", os.path.basename(origin))
            stats = import_db(path, ar, load_names())
            ar.db.execute("insert into imports (file, sha, ts, stats) values (?,?,?,?)",
                          (os.path.basename(origin), sha, int(time.time()), json.dumps(stats)))
            ar.commit()
            log("import done:", stats)
        try:
            export_secrets(path)
        except Exception as ex:
            log("message secrets export failed:", ex)
        migrate_user_data(ar)
    finally:  # never leave the plaintext decrypted db behind (even when the import fails)
        if path.endswith("msgstore.decrypted.db") and os.path.exists(path):
            os.remove(path)
    # thumbnails / waveforms for new media
    subprocess.run([sys.executable, os.path.join(waconf.TOOLS, "enrich.py")])


if __name__ == "__main__":
    main()
