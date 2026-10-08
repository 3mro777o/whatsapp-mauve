#!/usr/bin/env python3
"""Self-check of the phone-folder import into a docker-layout instance (temp dirs only): python3 tools/test_import.py"""
import os, shutil, sqlite3, subprocess, sys, tempfile
T = tempfile.mkdtemp(prefix="wa_import_")
HERE = os.path.dirname(os.path.realpath(__file__))
inst, phone = os.path.join(T, "inst"), os.path.join(T, "phone")
os.makedirs(inst)
open(os.path.join(inst, "wa.conf"), "w").write("WA_DB=archive.db\nWA_STORE=.\nWA_MEDIA_DIR=media\nWA_WEB=cache/web\nWA_USER=user\n"
                                                "WA_STATE=state\nWA_AUTH=state/auth\nWA_KEY_FILE=.wa_key\n")
env = {**os.environ, "WA_HOME": inst, "WA_PHONE_DIR": phone}
run = lambda *a: subprocess.run([sys.executable, os.path.join(HERE, *a[:1]), *a[1:]], env=env, capture_output=True, text=True)
db = os.path.join(T, "msgstore.db")
subprocess.run([sys.executable, os.path.join(HERE, "make_fake_phone.py"), phone, db], check=True)

r = run("import_msgstore.py", "--latest", "--no-mirror")  # no backups, no key: clean error, no traceback
assert r.returncode != 0 and "Traceback" not in r.stderr and "no msgstore" in r.stderr, r.stderr
os.makedirs(os.path.join(phone, "Databases")); open(os.path.join(phone, "Databases", "msgstore.db.crypt15"), "wb").write(b"x")
r = run("import_msgstore.py", "--latest", "--no-mirror")  # backup but no key
assert r.returncode != 0 and "no backup key" in r.stderr and "Traceback" not in r.stderr, r.stderr
open(os.path.join(inst, ".wa_key"), "w").write("abc")
r = run("import_msgstore.py", "--latest", "--no-mirror")  # invalid key
assert r.returncode != 0 and "64 hex" in r.stderr and "Traceback" not in r.stderr, r.stderr

r = run("import_msgstore.py", db)  # plain db: mirror + import
assert r.returncode == 0, r.stderr + r.stdout
M = os.path.join(inst, "media")
have = {os.path.relpath(os.path.join(a, f), M) for a, _, fs in os.walk(M) for f in fs}
assert have == {"WhatsApp Images/IMG-1.png", "WhatsApp Images/Sent/IMG-2.png", ".Statuses/s1.png",
                "WhatsApp AI Media/ai1.png", "WhatsApp Stickers/st1.png", "WhatsApp Video Notes/VID-1.mp4"}, have
c = sqlite3.connect(os.path.join(inst, "archive.db"))
rows = [str(r) for r in c.execute("select * from messages")]
assert any("media/WhatsApp Images/IMG-1.png" in x for x in rows) and any("media/WhatsApp Images/Sent/IMG-2.png" in x for x in rows), rows
assert any("media/WhatsApp Video Notes/VID-1.mp4" in x and "ptv" in x for x in rows), rows  # video note flag
assert not any("Media/" in x for x in rows), rows
assert os.stat(os.path.join(M, "WhatsApp Images/IMG-1.png")).st_ino == os.stat(os.path.join(phone, "Media/WhatsApp Images/IMG-1.png")).st_ino  # hardlink
shutil.rmtree(T)
print("import: ok")
