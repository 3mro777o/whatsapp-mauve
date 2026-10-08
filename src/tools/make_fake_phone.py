#!/usr/bin/env python3
"""Test fixture (temp dirs only): fake phone WhatsApp folder + tiny synthetic msgstore.db.
  make_fake_phone.py PHONE_DIR MSGSTORE_DB     then: import_msgstore.py MSGSTORE_DB  (WA_PHONE_DIR=PHONE_DIR)"""
import base64, os, sqlite3, sys
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
phone, dbp = sys.argv[1:3]
for f in ("Media/WhatsApp Images/IMG-1.png", "Media/WhatsApp Images/Sent/IMG-2.png", "Media/.Statuses/s1.png",
          "Media/WhatsApp AI Media/ai1.png", "Media/WhatsApp Stickers/st1.png",
          "Media/.Links/l1.png", "Media/.Wamo/w1.png", "Media/.wamocache/c1.png", "Media/.udDHabc/u1.png",
          "Media/WhatsApp Bug Report Attachments/b1.png", "Media/WhatsApp Images/.nomedia", "Media/WhatsApp Video Notes/VID-1.mp4"):
    p = os.path.join(phone, f); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(PNG)
if os.path.exists(dbp): os.remove(dbp)
d = sqlite3.connect(dbp)
d.executescript("""
create table jid(_id integer primary key, raw_string text);
create table chat(_id integer primary key, jid_row_id int, subject text, archived int, group_type int, created_timestamp int);
create table message(_id integer primary key, chat_row_id int, timestamp int, message_type int, from_me int, status int,
  sender_jid_row_id int, key_id text, text_data text, sort_id int);
create table message_media(message_row_id int, file_path text, mime_type text, width int, height int, media_name text,
  page_count int, file_size int, media_duration int, media_caption text);
create table call_log(_id integer primary key, video_call int, duration int, from_me int);
create table message_call_log(message_row_id int, call_log_row_id int);
create table message_system_chat_participant(message_row_id int, user_jid_row_id int);
create table message_add_on(_id integer primary key, parent_message_row_id int, from_me int, sender_jid_row_id int, timestamp int);
create table message_add_on_reaction(message_add_on_row_id int, reaction text);
insert into jid values (1, '15550001111@s.whatsapp.net');
insert into chat values (1, 1, null, 0, 0, 1700000000000);
insert into message values (1, 1, 1700000001000, 0, 0, 0, 1, 'K1', 'hello', 1);
insert into message values (2, 1, 1700000002000, 1, 0, 0, 1, 'K2', null, 2);
insert into message values (3, 1, 1700000003000, 1, 1, 13, 1, 'K3', null, 3);
insert into message values (4, 1, 1700000004000, 1, 0, 0, 1, 'K4', null, 4);
insert into message_media values (2, 'Media/WhatsApp Images/IMG-1.png', 'image/png', 1, 1, null, null, 70, null, 'cap');
insert into message_media values (3, 'Media/WhatsApp Images/Sent/IMG-2.png', 'image/png', 1, 1, null, null, 70, null, null);
insert into message values (5, 1, 1700000005000, 3, 0, 0, 1, 'K5', null, 5);
insert into message_media values (5, 'Media/WhatsApp Video Notes/VID-1.mp4', 'video/mp4', 1, 1, null, null, 70, 3, null);
insert into message_media values (4, 'Media/WhatsApp Images/IMG-missing.png', 'image/png', 1, 1, null, null, 70, null, null);
""")
d.commit()
