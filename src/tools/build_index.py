#!/usr/bin/env python3
"""Build archive/search.db (SQLite FTS5) from archive/chats.json."""
import json, sqlite3, os, re, html
from datetime import datetime
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/tools -> repo root
data = json.load(open(os.path.join(root, "archive/chats.json"), encoding="utf-8"))
db_path = os.path.join(root, "archive/search.db")
if os.path.exists(db_path): os.remove(db_path)
db = sqlite3.connect(db_path)
db.executescript("""
CREATE TABLE chats(jid TEXT PRIMARY KEY, name TEXT, html TEXT, n INTEGER);
CREATE TABLE messages(id INTEGER PRIMARY KEY, jid TEXT, chat TEXT, ts REAL, date TEXT,
  from_me INTEGER, sender TEXT, text TEXT, media TEXT);
CREATE VIRTUAL TABLE fts USING fts5(text, chat, sender, content='messages', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE INDEX m_jid_ts ON messages(jid, ts);
""")
tag = re.compile(r"<[^>]+>")
htmls = os.listdir(os.path.join(root, "archive/html"))
for jid, c in data.items():
    num = jid.split("@")[0]
    page = next((h for h in htmls if h.startswith(num + "-") or h == num + ".html"), None)
    name = c.get("name") or num
    db.execute("INSERT INTO chats VALUES(?,?,?,?)", (jid, name, page, len(c["messages"])))
    for m in c["messages"].values():
        text = " ".join(x for x in (m.get("data"), m.get("caption")) if isinstance(x, str))
        text = html.unescape(tag.sub(" ", text.replace("<br>", "\n"))).strip()
        media = m["data"] if m.get("media") and isinstance(m.get("data"), str) and "/" in m["data"] else None
        if media: text = (m.get("caption") or "").strip()
        ts = m.get("timestamp") or 0
        db.execute("INSERT INTO messages(jid,chat,ts,date,from_me,sender,text,media) VALUES(?,?,?,?,?,?,?,?)",
            (jid, name, ts, datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
             int(bool(m.get("from_me"))), "me" if m.get("from_me") else (m.get("sender") or name), text, media))
db.execute("INSERT INTO fts(fts) VALUES('rebuild')")
db.commit()
print(db.execute("select count(*) from messages").fetchone()[0], "messages indexed")
