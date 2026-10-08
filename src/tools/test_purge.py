#!/usr/bin/env python3
"""Self-check for purge + tombstones on a temp archive.db / store (never touches the real archive): python3 tools/test_purge.py"""
import os, shutil, sys, tempfile
T = tempfile.mkdtemp(prefix="wa_purge_")
os.environ.update(WA_STORE=os.path.join(T, "store"), WA_WEB=os.path.join(T, "web"), WA_STATE=os.path.join(T, "state"))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import archive_db

os.makedirs(os.path.join(T, "store", "Media"))
for n in ("one.jpg", "shared.jpg"): open(os.path.join(T, "store", "Media", n), "wb").write(b"x")
ar = archive_db.Archive(os.path.join(T, "t.db"))
cid = ar.ensure_chat("111@s.whatsapp.net")
a = ar.upsert_message(cid, "K1", 0, None, 10, "img", "", {"m": "Media/one.jpg"}, "live")
b = ar.upsert_message(cid, "K2", 1, None, 11, "img", "", {"m": "Media/shared.jpg"}, "live")
c = ar.upsert_message(cid, "K3", 1, None, 12, "img", "", {"m": "Media/shared.jpg"}, "live")
ar.react(a, "me", "👍", 1)
ar.commit()
r = archive_db.purge_messages(ar.db, [a, b], media=True)
assert r == {"removed": 2, "files": 1}, r  # shared.jpg stays: K3 still uses it
assert os.path.exists(os.path.join(T, "store", "Media", "shared.jpg")) and not os.path.exists(os.path.join(T, "store", "Media", "one.jpg"))
assert ar.db.execute("select count(*) from reactions").fetchone()[0] == 0
assert ar.upsert_message(cid, "K1", 0, None, 10, "img", "", {}, "history") is None      # live/history sync cannot bring it back
assert ar.upsert_message("other", "K2", 1, None, 11, "img", "", {}, "backup") is None   # nor the importer, even under another chat id
assert ar.revoke(cid, "K1", 0, 20, "me", "live") is None                                # nor a revoke placeholder
assert ar.upsert_message(cid, "K1", 1, None, 10, "img", "", {}, "live")                 # same key id from the other side is a different message
assert archive_db.purge_messages(ar.db, [c], media=True)["files"] == 1
shutil.rmtree(T)
print("purge: ok")
