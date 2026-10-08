-- archive.db: cumulative WhatsApp archive. Rows are only deleted by an explicit purge (tombstone in `purged`).
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS chats (
  id          TEXT PRIMARY KEY,          -- stable id: jid user part ("4917612345678", "120363…", "status")
  jid         TEXT NOT NULL UNIQUE,      -- canonical jid (phone form when known)
  alt         TEXT,                      -- alternative jid (lid <-> phone)
  type        TEXT,                      -- direct|group|channel|community|announce|status|broadcast
  subject     TEXT,                      -- group/channel subject
  parent_jid  TEXT,                      -- community parent (groups)
  archived    INTEGER DEFAULT 0,
  created     INTEGER,
  group_type  INTEGER,
  msgstore_id INTEGER,                   -- last seen msgstore chat._id (for migrations)
  merged_into TEXT,                      -- set when a lid chat was merged into a phone chat
  meta        TEXT,                      -- JSON (desc, size, announce, …)
  updated     INTEGER,
  unread      INTEGER DEFAULT 0,         -- unread incoming messages (wa-sync tracking; POST /api/read resets locally)
  read_ts     INTEGER                    -- when the unread count last became 0
);

CREATE TABLE IF NOT EXISTS aliases (      -- lid jid -> phone jid
  jid   TEXT PRIMARY KEY,
  canon TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_renames (  -- old chat id -> new chat id (lid chat became phone chat …)
  old TEXT PRIMARY KEY,
  new TEXT NOT NULL,
  ts  INTEGER
);

CREATE TABLE IF NOT EXISTS contacts (
  jid      TEXT PRIMARY KEY,               -- canonical jid
  name     TEXT,                           -- address book / vCard name
  push     TEXT,                           -- name the person set themselves
  verified TEXT,                           -- business verified name
  updated  INTEGER
);

CREATE TABLE IF NOT EXISTS group_members (
  chat_id TEXT NOT NULL,
  member  TEXT NOT NULL,                   -- canonical jid
  admin   INTEGER DEFAULT 0,
  active  INTEGER DEFAULT 1,               -- 0 = left/removed (kept for history)
  updated INTEGER,
  PRIMARY KEY (chat_id, member)
);

CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY,
  chat_id    TEXT NOT NULL,
  key_id     TEXT NOT NULL,
  from_me    INTEGER NOT NULL DEFAULT 0,
  sender     TEXT,                         -- canonical jid of the author (NULL = chat partner / me)
  ts         INTEGER NOT NULL,             -- seconds
  sort       INTEGER DEFAULT 0,            -- tie-breaker inside the same second
  kind       TEXT,                         -- NULL=text, img, vid, ptt, doc, sticker, loc, poll, call, sys, sys2, del
  text       TEXT,                         -- current text / caption ("x")
  data       TEXT,                         -- JSON: remaining viewer fields (m, wh, d, n, sz, q, lk, …)
  revoked    INTEGER DEFAULT 0,
  edited     INTEGER DEFAULT 0,
  text_ts    INTEGER,                      -- since when the current text is valid
  source     TEXT,                         -- backup|live|history
  first_seen INTEGER,
  updated    INTEGER,
  enrich     INTEGER DEFAULT 0,            -- 0 = thumbnails/waveform pending
  UNIQUE (chat_id, key_id, from_me)
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages (chat_id, ts, sort);
CREATE INDEX IF NOT EXISTS messages_key ON messages (key_id);
CREATE INDEX IF NOT EXISTS messages_enrich ON messages (enrich) WHERE enrich = 0;

CREATE TABLE IF NOT EXISTS message_versions (  -- every previous text/caption
  id       INTEGER PRIMARY KEY,
  msg_id   INTEGER NOT NULL,
  ts       INTEGER,                         -- when this version was current from
  text     TEXT,
  source   TEXT,
  recorded INTEGER
);
CREATE INDEX IF NOT EXISTS versions_msg ON message_versions (msg_id);

CREATE TABLE IF NOT EXISTS revocations (
  msg_id     INTEGER PRIMARY KEY,
  deleted_at INTEGER,
  by         TEXT,                          -- canonical jid of whoever deleted it (admin or sender)
  source     TEXT,
  recorded   INTEGER
);

CREATE TABLE IF NOT EXISTS reactions (
  msg_id  INTEGER NOT NULL,
  reactor TEXT NOT NULL,                    -- canonical jid, or "me"
  emoji   TEXT,                             -- '' = removed
  ts      INTEGER,
  PRIMARY KEY (msg_id, reactor)
);

CREATE TABLE IF NOT EXISTS media (            -- media download state (live sync)
  msg_id     INTEGER PRIMARY KEY,
  path       TEXT,                          -- relative to the store ("Media/…")
  size       INTEGER,
  mime       TEXT,
  downloaded INTEGER DEFAULT 0,             -- 1 = file in store, 0 = only keys stored (too big / failed)
  state      TEXT,                          -- done|pending (too big, waiting for the user)|queued|downloading|failed|skipped
  progress   REAL,                          -- 0..1 while downloading
  name       TEXT,                          -- file name for display
  kind       TEXT,                          -- img|vid|doc|ptt|sticker
  info       TEXT,                          -- JSON: mediaKey, directPath, url, type, fileEncSha256, …
  error      TEXT,
  created    INTEGER,
  updated    INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (               -- requests from the viewer to the sync service
  id      INTEGER PRIMARY KEY,
  kind    TEXT NOT NULL,                     -- media
  arg     TEXT,
  state   TEXT DEFAULT 'queued',             -- queued|running|done|error
  result  TEXT,
  created INTEGER,
  updated INTEGER
);

CREATE TABLE IF NOT EXISTS changes (            -- append-only change feed for the viewer
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT,
  ts      INTEGER
);

CREATE TABLE IF NOT EXISTS sync_state (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS imports (
  id    INTEGER PRIMARY KEY,
  file  TEXT,
  sha   TEXT,
  ts    INTEGER,
  stats TEXT
);

CREATE TABLE IF NOT EXISTS purged (              -- tombstones: messages the user purged; no insert path may bring them back
  chat_id TEXT NOT NULL,
  key_id  TEXT NOT NULL,
  from_me INTEGER NOT NULL DEFAULT 0,
  ts      INTEGER,                               -- when it was purged
  PRIMARY KEY (chat_id, key_id, from_me)
);
CREATE INDEX IF NOT EXISTS purged_key ON purged (key_id, from_me);
