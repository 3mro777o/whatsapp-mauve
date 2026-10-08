#!/usr/bin/env node
// wa-sync: read-only, invisible WhatsApp linked device that archives everything into archive.db.
//
//   node sync.mjs            run the service (systemd: wa-sync.service)
//   node sync.mjs --pair     interactive: show a QR code to link this device (stop the service first)
//
// Sends only through the local send API at the bottom (127.0.0.1 + token); never read receipts.
// Presence is set to "unavailable" so the phone keeps notifying.
// All database writes go through tools/archive_writer.py (JSON lines on its stdin).
import makeWASocket, {
  useMultiFileAuthState, DisconnectReason, Browsers, downloadMediaMessage, normalizeMessageContent,
  getContentType, WAMessageStubType, proto, jidNormalizedUser,
} from "@whiskeysockets/baileys";
import pino from "pino";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { pipeline } from "node:stream/promises";
import { Transform } from "node:stream";
import crypto from "node:crypto";
import http from "node:http";

// ---------------- config (wa.conf, same rules as tools/waconf.py) ----------------
const TOOLS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOME = path.resolve(process.env.WA_HOME || path.dirname(path.dirname(TOOLS)));  // src/tools -> repo root
const DEFAULTS = { WA_STATE: "state", WA_AUTH: "state/auth", WA_STORE: "archive/store", WA_MEDIA_MAX_MB: "200",
  WA_DOWNLOAD_STATUS: "1", WA_PYTHON: "", WA_SYNC_PORT: "8778", WA_WEB: "archive/web", WA_USER: "", WA_MEDIA_DIR: "Media" };
function loadConf() {
  const c = { ...DEFAULTS };
  try {
    for (let line of fs.readFileSync(path.join(HOME, "wa.conf"), "utf8").split("\n")) {
      line = line.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) continue;
      const i = line.indexOf("=");
      c[line.slice(0, i).trim()] = line.slice(i + 1).split(" #")[0].trim().replace(/^["']|["']$/g, "");
    }
  } catch {}
  for (const [k, v] of Object.entries(process.env)) if (k.startsWith("WA_")) c[k] = v;
  return c;
}
const CONF = loadConf();
const P = k => (path.isAbsolute(CONF[k]) ? CONF[k] : path.join(HOME, CONF[k]));
const STATE = P("WA_STATE"), AUTH = P("WA_AUTH"), STORE = P("WA_STORE");
const MEDIA_DIR = CONF.WA_MEDIA_DIR.replace(/^\/+|\/+$/g, "") || "Media";
const USER_DIR = CONF.WA_USER ? P("WA_USER") : path.join(P("WA_WEB"), "user");
const MAX_BYTES = Number(CONF.WA_MEDIA_MAX_MB) * 1024 * 1024;
const DOWNLOAD_STATUS = CONF.WA_DOWNLOAD_STATUS !== "0";
const PYTHON = CONF.WA_PYTHON || "python3";
const PAIR = process.argv.includes("--pair");
fs.mkdirSync(STATE, { recursive: true });

const logger = pino({ level: process.env.WA_LOG || "warn" });
const log = (...a) => console.log(new Date().toISOString(), ...a);

// ---------------- status file + notifications ----------------
const STATUS = path.join(STATE, "status.json");
// ---------------- message secrets (needed to decrypt edits sent as secretEncryptedMessage) ----------------
// Every message carries messageContextInfo.messageSecret; edits are AES-GCM encrypted with a key derived
// from the ORIGINAL message's secret (same scheme as whatsmeow msgsecret.go, use case "Message Edit").
const SECRETS_FILE = path.join(STATE, "msg-secrets.jsonl");
const secrets = new Map();  // key_id -> {s: base64 secret, a: author jid}
function loadSecrets() {
  try {
    for (const line of fs.readFileSync(SECRETS_FILE, "utf8").split("\n"))
      if (line) { try { const o = JSON.parse(line); secrets.set(o.k, o); } catch {} }
  } catch {}
}
loadSecrets();
// import_msgstore.py appends secrets from new backups and sends SIGHUP
process.on("SIGHUP", () => { const n = secrets.size; loadSecrets(); log(`secrets reloaded: +${secrets.size - n}`); });
function rememberSecret(keyId, secret, author) {
  if (!keyId || !secret || !secret.length || secrets.has(keyId)) return;
  const o = { k: keyId, s: Buffer.from(secret).toString("base64"), a: author ? jidNormalizedUser(author) : null };
  secrets.set(keyId, o);
  try { fs.appendFileSync(SECRETS_FILE, JSON.stringify(o) + "\n", { mode: 0o600 }); } catch {}
}
function hkdf(secret, info) {
  return Buffer.from(crypto.hkdfSync("sha256", secret, Buffer.alloc(0), info, 32));
}
function decryptSecretEdit(sem, editorCandidates) {
  const target = sem.targetMessageKey?.id;
  const rec = target && secrets.get(target);
  if (!rec) return { error: "no secret for target " + target };
  const secret = Buffer.from(rec.s, "base64");
  const payload = Buffer.from(sem.encPayload), iv = Buffer.from(sem.encIv);
  const authors = [...new Set([rec.a, ...editorCandidates].filter(Boolean))];
  for (const orig of authors) for (const mod of [...new Set([...editorCandidates, orig])]) {
    try {
      const key = hkdf(secret, Buffer.concat([Buffer.from(target), Buffer.from(orig), Buffer.from(mod), Buffer.from("Message Edit")]));
      const d = crypto.createDecipheriv("aes-256-gcm", key, iv);
      d.setAuthTag(payload.subarray(payload.length - 16));
      const plain = Buffer.concat([d.update(payload.subarray(0, payload.length - 16)), d.final()]);
      return { target, message: proto.Message.decode(plain) };
    } catch {}
  }
  return { error: "decrypt failed for target " + target };
}

let status = { state: "starting", since: now(), last: now(), msg: null, me: null };
function now() { return Math.floor(Date.now() / 1000); }
function setStatus(state, msg = null, extra = {}) {
  if (state !== status.state) status.since = now();
  status = { ...status, ...extra, state, msg, last: now(), pid: process.pid };
  const tmp = STATUS + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(status));
  fs.renameSync(tmp, STATUS);
}
function notify(title, body, urgency = "normal") {
  const bus = `/run/user/${os.userInfo().uid}/bus`;
  const env = { ...process.env };
  if (!env.DBUS_SESSION_BUS_ADDRESS) {
    if (!fs.existsSync(bus)) return;
    env.DBUS_SESSION_BUS_ADDRESS = `unix:path=${bus}`;
  }
  try { spawn("notify-send", ["-a", "WhatsApp Archive", "-u", urgency, "-i", "dialog-information", title, body], { env, stdio: "ignore" }).on("error", () => {}); } catch {}
}

// ---------------- writer (python) ----------------
let writer, writerBuf = "";
function startWriter() {
  writer = spawn(PYTHON, [path.join(TOOLS, "archive_writer.py")], { stdio: ["pipe", "pipe", "inherit"], env: { ...process.env, WA_HOME: HOME } });
  writer.stdout.setEncoding("utf8");
  writer.stdout.on("data", d => {
    writerBuf += d;
    let i;
    while ((i = writerBuf.indexOf("\n")) >= 0) {
      const line = writerBuf.slice(0, i); writerBuf = writerBuf.slice(i + 1);
      try { const m = JSON.parse(line); if (m.job) onJob(m.job); } catch (e) { log("writer msg error", e.message); }
    }
  });
  writer.on("exit", code => { log("writer exited", code); if (!shuttingDown) setTimeout(startWriter, 2000); });
}
function W(ev) { if (writer?.stdin.writable) writer.stdin.write(JSON.stringify(ev) + "\n"); }

// ---------------- jid helpers ----------------
const num = v => (v == null ? 0 : typeof v === "number" ? v : typeof v === "object" && v.toNumber ? v.toNumber() : Number(v));
const isLid = j => typeof j === "string" && j.endsWith("@lid");
const isPn = j => typeof j === "string" && j.endsWith("@s.whatsapp.net");
const knownAlias = new Map();
function alias(a, b) {
  if (!a || !b) return;
  a = jidNormalizedUser(a); b = jidNormalizedUser(b);
  const [lid, pn] = isLid(a) && isPn(b) ? [a, b] : isLid(b) && isPn(a) ? [b, a] : [];
  if (!lid || knownAlias.get(lid) === pn) return;
  knownAlias.set(lid, pn);
  W({ op: "alias", lid, pn });
}
const b64 = v => (v ? Buffer.from(v).toString("base64") : undefined);

// ---------------- message conversion ----------------
const MEDIA_TYPES = { imageMessage: "img", videoMessage: "vid", audioMessage: "ptt", documentMessage: "doc", stickerMessage: "sticker" };
const DL_TYPE = { img: "image", vid: "video", ptt: "audio", doc: "document", sticker: "sticker" };
const EXT = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "video/mp4": "mp4",
  "audio/ogg": "ogg", "audio/ogg; codecs=opus": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "application/pdf": "pdf" };
const STUB_NAME = Object.fromEntries(Object.entries(WAMessageStubType).filter(([k, v]) => typeof v === "number").map(([k, v]) => [v, k]));

function quoteOf(ci) {
  if (!ci?.stanzaId) return null;
  const qm = normalizeMessageContent(ci.quotedMessage);
  const qt = qm ? getContentType(qm) : null;
  const c = qt ? qm[qt] : null;
  const text = typeof c === "string" ? c : c?.text || c?.caption ||
    ({ imageMessage: "📷 صورة", videoMessage: "🎥 فيديو", ptvMessage: "🎥 فيديو", audioMessage: "🎤 رسالة صوتية", documentMessage: "📄 مستند", stickerMessage: "ملصق" }[qt]) || "رسالة";
  return { id: ci.stanzaId, jid: ci.participant ? jidNormalizedUser(ci.participant) : null, text, from_me: false };
}

function chatJidOf(key) {
  let j = jidNormalizedUser(key.remoteJid);
  if (isLid(j) && key.remoteJidAlt && isPn(jidNormalizedUser(key.remoteJidAlt))) { alias(j, key.remoteJidAlt); j = jidNormalizedUser(key.remoteJidAlt); }
  return j;
}
function senderOf(key, chatJid) {
  if (key.fromMe) return null;
  let s = key.participant ? jidNormalizedUser(key.participant) : chatJid;
  if (key.participantAlt) { alias(s, key.participantAlt); if (isLid(s) && isPn(jidNormalizedUser(key.participantAlt))) s = jidNormalizedUser(key.participantAlt); }
  return s;
}

function meJids() { return [sock?.user?.lid, sock?.user?.id].filter(Boolean).map(j => jidNormalizedUser(j)); }

/** returns {ev, media:{type,content}|null} or null if nothing to store */
function convert(msg, source) {
  const key = msg.key;
  if (!key?.remoteJid || !key.id) return null;
  const chat_jid = chatJidOf(key);
  if (chat_jid.endsWith("@newsletter") && !msg.message) return null;
  const sender = senderOf(key, chat_jid);
  const ts = num(msg.messageTimestamp) || now();
  if (msg.pushName && sender && !key.fromMe) W({ op: "contact", jid: sender, push: msg.pushName });
  const base = { op: "msg", chat_jid, key_id: key.id, from_me: key.fromMe ? 1 : 0, sender, ts, source };

  // group/system notifications
  if (!msg.message && msg.messageStubType) {
    const st = STUB_NAME[msg.messageStubType];
    if (st === "REVOKE") return { ev: { op: "revoke", chat_jid, key_id: key.id, from_me: key.fromMe ? 1 : 0, ts, by: sender || "me", source } };
    if (st === "CALL_MISSED_VOICE" || st === "CALL_MISSED_VIDEO")
      return { ev: { ...base, kind: "call", text: "مكالمة فائتة", data: { v: st.endsWith("VIDEO") ? 1 : 0, d: 0 } } };
    const params = (msg.messageStubParameters || []).map(p => (typeof p === "string" && p.includes("@") ? jidNormalizedUser(p) : p));
    return { ev: { ...base, stub: { type: st, actor: msg.participant ? jidNormalizedUser(msg.participant) : sender, params } } };
  }
  const mci = msg.message?.messageContextInfo;
  if (mci?.messageSecret) rememberSecret(key.id, mci.messageSecret, key.fromMe ? meJids()[0] : (key.participant || key.remoteJid));
  const content = normalizeMessageContent(msg.message);
  if (!content) return null;
  const type = getContentType(content);
  if (!type) return null;
  const c = content[type];

  if (type === "protocolMessage") {
    const t = c.type;
    if (t === proto.Message.ProtocolMessage.Type.REVOKE && c.key)
      return { ev: { op: "revoke", chat_jid, key_id: c.key.id, from_me: c.key.fromMe ? 1 : (key.fromMe ? 1 : 0), ts, by: key.fromMe ? "me" : sender, source } };
    if (t === proto.Message.ProtocolMessage.Type.MESSAGE_EDIT) log(`edit(upsert) chat=${chat_jid} target=${c.key?.id} hasContent=${!!c.editedMessage}`);
    if (t === proto.Message.ProtocolMessage.Type.REVOKE) log(`revoke(upsert) chat=${chat_jid} target=${c.key?.id}`);
    if (t === proto.Message.ProtocolMessage.Type.MESSAGE_EDIT && c.key && c.editedMessage) {
      const em = normalizeMessageContent(c.editedMessage), et = em && getContentType(em), ec = et && em[et];
      const text = typeof ec === "string" ? ec : ec?.text ?? ec?.caption;
      return text == null ? null : { ev: { op: "edit", chat_jid, key_id: c.key.id, from_me: c.key.fromMe ? 1 : 0, text, ts: num(c.timestampMs) / 1000 | 0 || ts, source } };
    }
    return null; // history-sync notifications, app state keys, ephemeral settings …
  }
  if (type === "reactionMessage") {
    if (!c.key) return null;
    return { ev: { op: "react", chat_jid, key_id: c.key.id, from_me: c.key.fromMe ? 1 : 0, reactor: key.fromMe ? "me" : sender,
      emoji: c.text || "", ts: Math.floor(num(c.senderTimestampMs) / 1000) || ts } };
  }
  if (type === "secretEncryptedMessage") {
    if (c.secretEncType !== proto.Message.SecretEncryptedMessage.SecretEncType.MESSAGE_EDIT) return null;
    const editors = key.fromMe ? meJids() : [key.participant, key.participantAlt, key.remoteJid, key.remoteJidAlt]
      .filter(j => j && !j.endsWith("@g.us")).map(j => jidNormalizedUser(j));
    const r = decryptSecretEdit(c, editors);
    if (r.error) { log("secret edit: " + r.error); return null; }
    const em = normalizeMessageContent(r.message), et = em && getContentType(em), ec = et && em[et];
    const pm = et === "protocolMessage" ? ec : null;  // usually wraps protocolMessage{MESSAGE_EDIT, editedMessage}
    const inner = pm?.editedMessage ? normalizeMessageContent(pm.editedMessage) : em;
    const it = inner && getContentType(inner), ic = it && inner[it];
    const text = typeof ic === "string" ? ic : ic?.text ?? ic?.caption;
    log(`secret edit decrypted target=${r.target} type=${et}/${it} hasText=${text != null}`);
    if (text == null) return null;
    return { ev: { op: "edit", chat_jid, key_id: r.target, from_me: c.targetMessageKey?.fromMe ? 1 : (key.fromMe ? 1 : 0), text, ts, source } };
  }
  if (["senderKeyDistributionMessage", "pollUpdateMessage", "messageContextInfo", "keepInChatMessage", "pinInChatMessage",
       "encReactionMessage", "encEventResponseMessage"].includes(type)) return null;

  const ev = { ...base, kind: null, text: null, data: {} };
  const d = ev.data;
  const ci = c?.contextInfo;
  const q = quoteOf(ci);
  if (q) ev.quote = q;
  let media = null;
  if (type === "conversation") ev.text = c;
  else if (type === "extendedTextMessage") {
    ev.text = c.text || "";
    if (c.matchedText || c.canonicalUrl) d.lk = [c.title || "", c.description || "", c.canonicalUrl || c.matchedText];
  } else if (MEDIA_TYPES[type] || type === "ptvMessage") {  // ptvMessage = round video note (same media as a video)
    const k = type === "ptvMessage" ? "vid" : MEDIA_TYPES[type];
    ev.kind = k;
    ev.text = c.caption || null;
    if (type === "videoMessage" && c.gifPlayback) d.gif = 1;
    if (type === "ptvMessage") d.ptv = 1;
    if (c.width && c.height) d.wh = [c.width, c.height];
    if (c.seconds) d.d = c.seconds;
    const size = num(c.fileLength);
    if (size) d.sz = size;
    if (k === "doc") { d.n = c.fileName || c.title || "مستند"; if (c.pageCount) d.pg = c.pageCount; }
    const m0 = msg.message || {};
    const viewOnce = m0.viewOnceMessage || m0.viewOnceMessageV2 || m0.viewOnceMessageV2Extension || c.viewOnce ||
      (k !== "sticker" && !(c.mediaKey && c.mediaKey.length));  // linked devices receive view-once media without a key
    if (viewOnce) { ev.kind = "sys2"; ev.text = `${k === "vid" ? "🎥" : k === "ptt" ? "🎤" : "📷"} رسالة للعرض مرة واحدة · بتنفتح على التلفون بس`; ev.data = { vo: 1 }; }
    else media = { k, size, c };
  } else if (type === "locationMessage" || type === "liveLocationMessage") {
    ev.kind = "loc"; ev.text = c.name || c.address || (type === "liveLocationMessage" ? "موقع مباشر" : "موقع");
    d.ll = [c.degreesLatitude, c.degreesLongitude];
  } else if (type === "contactMessage") ev.text = "👤 " + (c.displayName || "جهة اتصال");
  else if (type === "contactsArrayMessage") ev.text = "👤 " + (c.displayName || (c.contacts || []).map(x => x.displayName).join("، "));
  else if (/^pollCreationMessage/.test(type)) { ev.kind = "poll"; ev.text = c.name || ""; d.o = (c.options || []).map(o => [o.optionName, 0]); }
  else if (type === "eventMessage") ev.text = "📅 " + (c.name || "حدث");
  else if (type === "callLogMesssage" || type === "bcallMessage") { ev.kind = "call"; ev.text = "مكالمة"; }
  else if (type === "groupInviteMessage") ev.text = "📨 دعوة لمجموعة: " + (c.groupName || "");
  else if (type === "buttonsResponseMessage" || type === "listResponseMessage" || type === "templateButtonReplyMessage")
    ev.text = c.selectedDisplayText || c.title || "";
  else if (type === "productMessage") ev.text = "🛍️ " + (c.product?.title || "منتج");
  else return null;
  if (chat_jid === "status@broadcast" && !DOWNLOAD_STATUS) media = null;
  // Baileys derives the download type from the content key ("ptv"), which has no media path: download as videoMessage
  return { ev, media, msg: type === "ptvMessage" ? { ...msg, message: { videoMessage: c } } : msg };
}

// ---------------- media ----------------
const queue = [];
let active = 0;
function mediaInfo(m, k, c) {
  return { mediaKey: b64(c.mediaKey), directPath: c.directPath, url: c.url, mimetype: c.mimetype, type: DL_TYPE[k],
    fileEncSha256: b64(c.fileEncSha256), fileSha256: b64(c.fileSha256), fileLength: num(c.fileLength),
    mediaKeyTimestamp: num(c.mediaKeyTimestamp) || null, key: m.key, msgType: Object.keys(MEDIA_TYPES).find(t => MEDIA_TYPES[t] === k),
    ptt: k === "ptt" ? !!c.ptt : undefined, gif: c.gifPlayback ? 1 : undefined };
}
// live downloads land in the matching WhatsApp folder (same names as the phone, so the folder can be copied back as-is)
const WA_DIR = { img: "WhatsApp Images", vid: "WhatsApp Video", ptt: "WhatsApp Voice Notes", aud: "WhatsApp Audio", doc: "WhatsApp Documents", sticker: "WhatsApp Stickers" };
const WA_PFX = { img: "IMG", vid: "VID", ptt: "PTT", aud: "AUD", doc: "DOC", sticker: "STK" };
function relPathFor(ev, k, mime, fileName, fl = {}) {
  const d = new Date(ev.ts * 1000), y = d.getUTCFullYear(), p2 = n => String(n).padStart(2, "0");
  const ymd = `${y}${p2(d.getUTCMonth() + 1)}${p2(d.getUTCDate())}`;
  const wk = Math.floor(((d - Date.UTC(y, 0, 1)) / 864e5 + new Date(Date.UTC(y, 0, 1)).getUTCDay()) / 7) + 1;
  let ext = EXT[(mime || "").toLowerCase()] || (fileName && fileName.includes(".") ? fileName.split(".").pop().toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 8) : "")
    || (mime || "bin").split("/").pop().split(";")[0].replace(/[^a-z0-9]/g, "").slice(0, 8) || "bin";
  const safe = String(ev.key_id).replace(/[^A-Za-z0-9_-]/g, "_");
  const kk = k === "ptt" && fl.ptt === false ? "aud" : k;  // audioMessage without the voice-note flag = audio file
  let dir = ev.chat_jid === "status@broadcast" ? ".Statuses" : fl.gif ? "WhatsApp Animated Gifs" : WA_DIR[kk] || "WhatsApp Documents";
  if (kk === "ptt" && dir === WA_DIR.ptt) dir += `/${y}${p2(wk)}`;
  else if (ev.from_me && dir !== ".Statuses" && kk !== "sticker") dir += "/Sent";
  return `${MEDIA_DIR}/${dir}/${fl.gif ? "VID" : WA_PFX[kk] || "DOC"}-${ymd}-WA${safe}.${ext}`;
}
function handleMedia(conv, source) {
  const { ev, media } = conv;
  if (!media) return;
  const { k, size, c } = media;
  const info = mediaInfo(conv.msg, k, c);
  const name = k === "doc" ? (c.fileName || "مستند") : null;
  const base = { size: size || null, mime: c.mimetype, kind: k, name, info };
  const auto = source !== "history" && (!size || size <= MAX_BYTES);
  if (!auto) {
    const state = source === "history" ? "remote" : "pending";
    ev.media = { ...base, state };
    if (state === "pending") {
      const mb = (size / 1048576).toFixed(0);
      notify("WhatsApp: ملف كبير", `ملف كبير بانتظار التحميل: ${name || k} (${mb} MB)`, "normal");
    }
    return;
  }
  ev.media = { ...base, state: "auto" };
  queue.push({ ev, k, c, msg: conv.msg, info });
  pump();
}
function pump() {
  while (active < 2 && queue.length) {
    const job = queue.shift();
    active++;
    download(job).finally(() => { active--; pump(); });
  }
}
async function download({ ev, k, c, msg, info, jobId }) {
  const rel = relPathFor(ev, k, c?.mimetype || info?.mimetype, c?.fileName || info?.fileName, c ? { ptt: k === "ptt" ? !!c.ptt : undefined, gif: c.gifPlayback } : info);
  const abs = path.join(STORE, rel);
  const tmp = abs + ".part";
  const total = num(c?.fileLength) || info?.fileLength || 0;
  const ref = { chat_jid: ev.chat_jid, key_id: ev.key_id, from_me: ev.from_me };
  try {
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    const m = msg?.message ? msg : rebuild(info, ev);
    const stream = await downloadMediaMessage(m, "stream", {}, { logger, reuploadRequest: sock?.updateMediaMessage });
    let got = 0, lastReport = 0;
    const counter = new Transform({ transform(chunk, _, cb) {
      got += chunk.length;
      if (total && Date.now() - lastReport > 1000) { lastReport = Date.now(); W({ op: "media", ...ref, state: "downloading", progress: Math.min(0.99, got / total) }); }
      cb(null, chunk);
    } });
    await pipeline(stream, counter, fs.createWriteStream(tmp));
    fs.renameSync(tmp, abs);
    W({ op: "media", ...ref, state: "done", path: rel, size: got });
    if (jobId) W({ op: "job", id: jobId, state: "done", result: { m: rel } });
  } catch (e) {
    try { fs.rmSync(tmp, { force: true }); } catch {}
    const err = String(e?.message || e).slice(0, 200);
    log("media download failed", ev.key_id, err);
    W({ op: "media", ...ref, state: jobId ? "failed" : "remote", error: err });
    if (jobId) W({ op: "job", id: jobId, state: "error", result: err });
  }
}
function rebuild(info, ev) {
  const t = info.msgType || "documentMessage";
  const buf = s => (s ? Buffer.from(s, "base64") : undefined);
  return { key: info.key || { remoteJid: ev.chat_jid, id: ev.key_id, fromMe: !!ev.from_me },
    message: { [t]: { mediaKey: buf(info.mediaKey), directPath: info.directPath, url: info.url, mimetype: info.mimetype,
      fileEncSha256: buf(info.fileEncSha256), fileSha256: buf(info.fileSha256), fileLength: info.fileLength } } };
}
function onJob(job) {
  if (job.kind !== "media") return;
  const ev = { chat_jid: job.chat_jid, key_id: job.key_id, from_me: job.from_me, ts: now() };
  if (!sock || status.state !== "connected") { W({ op: "job", id: job.id, state: "queued" }); return; } // retried by writer later
  W({ op: "media", chat_jid: job.chat_jid, key_id: job.key_id, from_me: job.from_me, state: "downloading", progress: 0 });
  queue.unshift({ ev, k: job.msg_kind, c: null, msg: null, info: { ...job.info, fileLength: job.info.fileLength || job.size, fileName: job.name }, jobId: job.id });
  pump();
}

// ---------------- event processing ----------------
const liveSeen = new Map();  // chat jid -> ms of the last live +1 (a chats.upsert for the same message must not add it twice)
function processMessages(messages, source, notify = false) {
  for (const m of messages) {
    try {
      if (m.messageStubType && !m.message) log(`upsert stub=${m.messageStubType} id=${m.key?.id} params=${(m.messageStubParameters || []).slice(0, 1).join("|")}`);
      else if (m.message && (m.message.editedMessage || m.message.protocolMessage)) log(`upsert ${Object.keys(m.message).join(",")} id=${m.key?.id}`);
      const conv = convert(m, source);
      if (!conv) continue;
      if (conv.ev.op === "msg") handleMedia(conv, source);
      if (notify && conv.ev.op === "msg" && !conv.ev.stub && !/@(broadcast|newsletter)$/.test(conv.ev.chat_jid)) {
        conv.ev.unread = conv.ev.from_me ? 0 : 1;  // writer: +1 only for a new row, 0 when I answered (phone)
        liveSeen.set(conv.ev.chat_jid, Date.now());
      }
      W(conv.ev);
    } catch (e) { log("convert error", e.message); }
  }
}

async function syncGroups() {
  try {
    const groups = await sock.groupFetchAllParticipating();
    for (const g of Object.values(groups)) groupMeta(g, true);
    log(`groups: ${Object.keys(groups).length}`);
  } catch (e) { log("group fetch failed", e.message); }
}
function groupMeta(g, full) {
  if (!g?.id) return;
  const type = g.isCommunity ? "community" : g.isCommunityAnnounce ? "announce" : undefined;
  W({ op: "chat", jid: g.id, subject: g.subject, type, parent_jid: g.linkedParent || undefined,
    created: g.creation || undefined, meta: { desc: g.desc, size: g.size, announce: g.announce, restrict: g.restrict, owner: g.owner } });
  if (g.participants) {
    const members = g.participants.map(p => {
      if (p.phoneNumber) alias(p.id, p.phoneNumber);
      if (p.lid) alias(p.lid, p.id);
      const j = isLid(p.id) && p.phoneNumber ? p.phoneNumber : p.id;
      return { jid: jidNormalizedUser(j), admin: p.admin ? 1 : 0 };
    });
    W({ op: "members", chat_jid: g.id, members, full });
  }
}

// avatars: fill in missing ones once a day, slowly
async function avatarPass() {
  try {
    const dir = path.join(USER_DIR, "avatars");
    const have = new Set(fs.existsSync(dir) ? fs.readdirSync(dir).map(f => f.split(".")[0]) : []);
    const r = spawnSync(PYTHON, ["-c", "import sys,json;sys.path.insert(0,sys.argv[1]);import viewer_data;print(json.dumps([[c['jid'],c['j']] for c in json.loads(viewer_data.ViewerData().get('chats')) if c['type']!='channel']))", TOOLS], { encoding: "utf8", env: { ...process.env, WA_HOME: HOME } });
    const chats = JSON.parse(r.stdout || "[]").filter(([jid, j]) => !have.has(j) && !jid.endsWith("@broadcast"));
    let got = 0;
    for (const [jid] of chats) {
      if (status.state !== "connected") break;
      try {
        const url = await sock.profilePictureUrl(jid, "image", 20000);
        if (url) { const res = await fetch(url); if (res.ok) { W({ op: "avatar", jid, b64: Buffer.from(await res.arrayBuffer()).toString("base64") }); got++; } }
      } catch {}
      await new Promise(r => setTimeout(r, 1500 + Math.random() * 1000));
    }
    if (got) log(`avatars: ${got} new`);
  } catch (e) { log("avatar pass failed", e.message); }
}

// one avatar by jid -> writer (it keeps the old file in history/ when the picture differs). jid -> last fetch, max 1/hour for events
const avTried = new Map();
async function fetchAvatar(jid, { limit = true, win = 3600 } = {}) {
  if (typeof jid !== "string" || !jid.includes("@") || jid.endsWith("@broadcast") || jid.endsWith("@newsletter") || status.state !== "connected") return false;
  if (limit && now() - (avTried.get(jid) || 0) < win) return false;
  avTried.set(jid, now());
  try {
    const url = await sock.profilePictureUrl(jid, "image", 20000);
    if (!url) return false;
    const res = await fetch(url);
    if (!res.ok) return false;
    W({ op: "avatar", jid, b64: Buffer.from(await res.arrayBuffer()).toString("base64") });
    return true;
  } catch { return false; }  // no picture / hidden by privacy: keep what we have
}
const toPn = j => (isLid(j) ? knownAlias.get(j) || j : j);  // avatar files are named by phone number
async function ownAvatar() {
  const me = sock?.user?.id ? jidNormalizedUser(sock.user.id) : null;
  if (!me) return;
  try { fs.writeFileSync(path.join(STATE, "me.json"), JSON.stringify({ jid: me, name: sock.user.name || "" }), { mode: 0o600 }); } catch {}
  if (await fetchAvatar(me, { limit: false })) log("own avatar fetched");
}

// ---------------- connection ----------------
let sock = null, backoff = 2000, shuttingDown = false, heartbeat = null, avatarTimer = null, failures = 0;
async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH);
  if (!state.creds.me && !PAIR) {
    setStatus("logged_out", "الجهاز غير مربوط — شغّل wa-sync --pair");
    notify("WhatsApp Archive", "المزامنة متوقفة: الجهاز غير مربوط. شغّل wa-sync --pair", "critical");
    return; // stay idle (no restart loop); the check unit keeps reminding at login
  }
  setStatus("connecting");
  sock = makeWASocket({
    auth: state, logger, browser: Browsers.ubuntu("WA Archive"),
    markOnlineOnConnect: false, syncFullHistory: false, fireInitQueries: true,
    emitOwnEvents: true, generateHighQualityLinkPreview: false,
    getMessage: async () => undefined,
  });
  sock.ev.on("creds.update", saveCreds);
  // Raw stanzas: Baileys acks and drops view-once messages sent to linked devices ("unavailable" view_once),
  // so record a placeholder ourselves. Also log edit stanzas (ids only) to diagnose missed edits.
  sock.ws.on("CB:message", node => {
    try {
      const a = node.attrs || {}, kids = Array.isArray(node.content) ? node.content : [];
      const unavailable = kids.find(k => k.tag === "unavailable");
      const enc = kids.find(k => k.tag === "enc");
      if (a.edit) log(`raw edit=${a.edit} id=${a.id} from=${a.from} type=${a.type} category=${a.category || "-"} kids=${kids.map(k => k.tag + (k.attrs?.type ? ":" + k.attrs.type : "")).join(",")}`);
      if (!unavailable || !String(unavailable.attrs?.type || "").startsWith("view_once")) return;
      const meIds = [sock.user?.id, sock.user?.lid].filter(Boolean).map(j => jidNormalizedUser(j));
      const from = jidNormalizedUser(a.from), author = a.participant ? jidNormalizedUser(a.participant) : from;
      const fromMe = meIds.includes(author);
      const chatRaw = fromMe && !from.endsWith("@g.us") && a.recipient ? a.recipient : a.from;
      const key = { remoteJid: chatRaw, fromMe, id: a.id, participant: a.participant };
      const chat_jid = chatJidOf(key);
      const mt = enc?.attrs?.mediatype || unavailable.attrs?.mediatype || "";
      const icon = /video/.test(mt) ? "🎥" : /ptt|audio/.test(mt) ? "🎤" : "📷";
      log(`view-once id=${a.id} chat=${chat_jid} media=${mt || "?"}`);
      W({ op: "msg", chat_jid, key_id: a.id, from_me: fromMe ? 1 : 0, sender: fromMe ? null : senderOf(key, chat_jid),
          ts: Number(a.t) || now(), source: "live", kind: "sys2",
          text: `${icon} رسالة للعرض مرة واحدة · بتنفتح على التلفون بس`, data: { vo: 1 } });
    } catch (e) { log("raw message hook error", e.message); }
  });
  sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      if (PAIR) { const qrt = (await import("qrcode-terminal")).default; console.clear(); console.log("امسح الكود: واتساب ← الأجهزة المرتبطة ← ربط جهاز\n"); qrt.generate(qr, { small: true }); }
      else { setStatus("logged_out", "يلزم ربط الجهاز من جديد"); notify("WhatsApp Archive", "انفك ربط المزامنة. شغّل wa-sync --pair", "critical"); try { sock.end(undefined); } catch {} }
      return;
    }
    if (connection === "open") {
      backoff = 2000; failures = 0;
      const me = sock.user?.id ? jidNormalizedUser(sock.user.id) : null;
      setStatus("connected", null, { me });
      log("connected as", me);
      // explicit "unavailable" (raw node: sendPresenceUpdate() would attach a push name, which we never set)
      try { await sock.sendNode({ tag: "presence", attrs: { type: "unavailable" } }); } catch (e) { log("presence", e.message); }
      if (PAIR) { log("تم الربط. شغّل الخدمة: systemctl --user start wa-sync"); setTimeout(() => process.exit(0), 5000); return; }
      setTimeout(syncGroups, 5000);
      clearTimeout(avatarTimer);
      avatarTimer = setTimeout(function loop() { ownAvatar().then(avatarPass).finally(() => { avatarTimer = setTimeout(loop, 24 * 3600e3); }); }, 120e3);
      setTimeout(ownAvatar, 8000);  // now (reconnects too), then daily with the loop above
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        setStatus("logged_out", "تم فك ربط الجهاز من التلفون");
        notify("WhatsApp Archive", "تم فك ربط جهاز الأرشفة من التلفون. المزامنة متوقفة — شغّل wa-sync --pair", "critical");
        return; // keep the auth folder (never deleted automatically)
      }
      if (shuttingDown) return;
      failures++;
      setStatus("disconnected", `connection closed (${code ?? "?"})`);
      if (failures === 5) notify("WhatsApp Archive", "المزامنة عم تفشل بالاتصال بشكل متكرر", "normal");
      log(`connection closed (${code}); reconnecting in ${backoff / 1000}s`);
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 120000);
    }
  });
  sock.ev.on("messages.upsert", ({ messages, type }) => processMessages(messages, "live", type === "notify"));
  sock.ev.on("messages.update", updates => {
    const read = {};  // chat -> ids of incoming messages I read elsewhere (read-self)
    for (const { key, update } of updates) {
      if (!key?.remoteJid) continue;
      const chat_jid = chatJidOf(key);
      if (!key.fromMe && update.status >= proto.WebMessageInfo.Status.READ && !key.remoteJid.endsWith("@g.us")) (read[chat_jid] ||= []).push(key.id);
      if (update.messageStubType === WAMessageStubType.REVOKE || (update.message === null && update.messageStubType)) {
        W({ op: "revoke", chat_jid, key_id: key.id, from_me: key.fromMe ? 1 : 0, ts: now(), by: key.fromMe ? "me" : senderOf(key, chat_jid), source: "live" });
      }
      const em = update.message?.editedMessage?.message || update.message?.editedMessage;
      if (em || update.messageStubType) log(`update chat=${chat_jid} key=${key.id} edit=${!!em} stub=${update.messageStubType ?? "-"}`);
      if (em) {
        const n = normalizeMessageContent(em), t = n && getContentType(n), c = t && n[t];
        const pm = t === "protocolMessage" ? c : null;
        const target = pm?.key?.id || key.id;
        const inner = pm?.editedMessage ? normalizeMessageContent(pm.editedMessage) : n;
        const it = inner && getContentType(inner), ic = it && inner[it];
        const text = typeof ic === "string" ? ic : ic?.text ?? ic?.caption;
        if (text != null) W({ op: "edit", chat_jid, key_id: target, from_me: key.fromMe ? 1 : 0, text, ts: now(), source: "live" });
      }
    }
    for (const [chat, ids] of Object.entries(read)) readSelf(chat, ids, "msg.update");
  });
  sock.ev.on("messaging-history.set", ({ chats, contacts, messages, lidPnMappings }) => {
    for (const m of lidPnMappings || []) alias(m.lid, m.pn);
    for (const c of contacts || []) contactEv(c);
    for (const c of chats || []) if (c.id && (c.name || c.archived != null)) W({ op: "chat", jid: c.id, subject: c.id.endsWith("@g.us") || c.id.endsWith("@newsletter") ? c.name : undefined, archived: c.archived ? 1 : undefined });
    for (const c of chats || []) upUnread(c);
    processMessages(messages || [], "history");
    log(`history: ${messages?.length || 0} messages, ${chats?.length || 0} chats`);
  });
  sock.ev.on("lid-mapping.update", m => alias(m.lid, m.pn));
  const contactEv = c => {
    if (!c?.id) return;
    if (c.lid && c.phoneNumber) alias(c.lid, c.phoneNumber);
    else if (c.phoneNumber) alias(c.id, c.phoneNumber);
    else if (c.lid) alias(c.lid, c.id);
    const jid = isLid(c.id) && c.phoneNumber ? c.phoneNumber : c.id;
    if (c.name || c.notify || c.verifiedName) W({ op: "contact", jid, name: c.name, push: c.notify, verified: c.verifiedName });
  };
  sock.ev.on("contacts.upsert", cs => cs.forEach(contactEv));
  sock.ev.on("contacts.update", cs => cs.forEach(c => {
    contactEv(c);
    if (c?.id && (c.imgUrl === "changed" || c.imgUrl === "removed")) {  // picture notification (also fired for groups)
      const jid = toPn(jidNormalizedUser(c.id));
      if (c.imgUrl === "removed") W({ op: "avatar", jid, remove: 1 });
      else fetchAvatar(jid, { win: 60 }).then(ok => ok && log(`avatar changed ${jid.split("@")[0]}`));
    }
  }));
  sock.ev.on("groups.upsert", gs => gs.forEach(g => groupMeta(g, true)));
  sock.ev.on("groups.update", gs => gs.forEach(g => groupMeta(g, false)));
  sock.ev.on("group-participants.update", ({ id, participants, action }) => {
    for (const p of participants || []) {
      const pid = typeof p === "string" ? p : p.id, pn = typeof p === "object" ? p.phoneNumber : null;
      if (pn) alias(pid, pn);
      const jid = jidNormalizedUser(isLid(pid) && pn ? pn : pid);
      if (action === "add") W({ op: "member", chat_jid: id, jid, active: 1 });
      else if (action === "remove") W({ op: "member", chat_jid: id, jid, active: 0 });
      else if (action === "promote") W({ op: "member", chat_jid: id, jid, active: 1, admin: 1 });
      else if (action === "demote") W({ op: "member", chat_jid: id, jid, active: 1, admin: 0 });
    }
  });
  // unread: chats.upsert / history carry the absolute count; chats.update's positive unreadCount is a +1 delta (we count
  // messages ourselves), but 0 = read on another device and <0 = marked unread there.
  const upUnread = c => {
    if (!c?.id || typeof c.unreadCount !== "number" || c.id.endsWith("@broadcast") || c.id.endsWith("@newsletter")) return;
    if (Date.now() - (liveSeen.get(jidNormalizedUser(c.id)) || 0) < 10000) return;
    W({ op: "unread", jid: jidNormalizedUser(c.id), n: Math.max(c.unreadCount, 0) || (c.unreadCount < 0 ? 1 : 0), up: 1 });
  };
  sock.ev.on("chats.upsert", cs => cs.forEach(upUnread));
  // I read messages on another device (the phone): 1:1 = messages.update status>=READ on an incoming key ('read-self' receipt),
  // group = message-receipt.update with my own userJid + readTimestamp. Writer lowers unread to the incoming msgs newer than those.
  const isMe = j => !!j && [sock.user?.id, sock.user?.lid].filter(Boolean).some(m => jidNormalizedUser(m) === jidNormalizedUser(j));
  const readSelf = (chat, ids, via) => {
    if (!chat || /@(broadcast|newsletter)$/.test(chat) || !ids.length) return;
    log(`read-sync ${via} chat=${chat} ids=${ids.length}`);
    W({ op: "unread", jid: jidNormalizedUser(chat), upto: ids.slice(0, 200) });
  };
  sock.ev.on("message-receipt.update", us => {
    const by = {};
    for (const { key, receipt } of us) if (key?.remoteJid && receipt?.readTimestamp && isMe(receipt.userJid)) (by[key.remoteJid] ||= []).push(key.id);
    for (const [chat, ids] of Object.entries(by)) readSelf(chat, ids, "receipt");
  });
  sock.ws.on("CB:notification,type:server_sync", n => log(`read-sync server_sync ${(Array.isArray(n.content) ? n.content : []).map(k => k.attrs?.name).join(",")}`));
  sock.ev.on("chats.update", us => {
    for (const u of us) {
      if (!u.id) continue;
      if (u.unreadCount === null || u.unreadCount <= 0) log(`read-sync chats.update chat=${u.id} unreadCount=${u.unreadCount}`);
      if (u.archived != null) W({ op: "chat", jid: u.id, archived: u.archived ? 1 : 0 });
      if (u.unreadCount === 0 && !u.id.endsWith("@broadcast")) W({ op: "unread", jid: jidNormalizedUser(u.id), n: 0 });
      else if (u.unreadCount < 0) W({ op: "unread", jid: jidNormalizedUser(u.id), mark: 1 });
    }
  });
}

function shutdown(sig) {
  shuttingDown = true;
  log("stopping", sig);
  setStatus("stopped", sig);
  try { sock?.end(undefined); } catch {}
  try { writer?.stdin.end(); } catch {}
  setTimeout(() => process.exit(0), 1500);
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("unhandledRejection", e => log("unhandled", e?.message || e));

// ---------------- local send API (wa-archive proxies here) ----------------
// 127.0.0.1 only; every request needs X-WA-Token = state/api-token (constant-time compare).
//   POST /send {jid, type, text?, file?, name?, mime?, seconds?, reply?} -> {ok, key_id} | {ok:false, error}
//   POST /delete {jid, key_id, from_me, participant?, scope: "me"|"everyone", ts} -> {ok}   (everyone = revoke; me = deleteForMe app-state)
//   POST /react {jid, key_id, from_me, participant?, emoji ("" removes)} -> {ok, key_id}
//   POST /read {jid, key_id, from_me, participant?, ts} -> {ok}   (latest message of the chat; app-state markChatAsRead, NO read receipt)
//   GET  /health -> {ok, connected}
// file must be a real file inside state/outbox (realpath check). Never marks online or read.
const OUTBOX = path.join(STATE, "outbox");
fs.mkdirSync(OUTBOX, { recursive: true, mode: 0o700 });
const SEND_TYPES = new Set(["text", "image", "video", "document", "ptt", "audio", "sticker", "gif"]);
function tokenOk(got) {
  let want = "";
  try { want = fs.readFileSync(path.join(STATE, "api-token"), "utf8").trim(); } catch {}
  const a = Buffer.from(String(got || "")), b = Buffer.from(want);
  return b.length > 0 && a.length === b.length && crypto.timingSafeEqual(a, b);
}
function outboxFile(p) {
  if (typeof p !== "string" || !path.isAbsolute(p)) return null;
  try {
    const real = fs.realpathSync(p);
    return real.startsWith(fs.realpathSync(OUTBOX) + path.sep) && fs.statSync(real).isFile() ? real : null;
  } catch { return null; }
}
function readBody(req, max) {
  return new Promise((resolve, reject) => {
    const parts = []; let n = 0;
    req.on("data", c => { n += c.length; if (n <= max) parts.push(c); });
    req.on("end", () => (n > max ? reject(new Error("body too large")) : resolve(Buffer.concat(parts).toString("utf8"))));
    req.on("error", reject);
  });
}
function sendOne({ jid, type, text, file, name, mime, seconds, reply }) {
  const opts = reply?.key_id ? { quoted: { key: { remoteJid: jid, id: String(reply.key_id), fromMe: !!reply.from_me,
    participant: reply.sender_jid || undefined }, message: { conversation: String(reply.text || "") } } } : {};
  const caption = text ? { caption: text } : {};
  const secs = Number(seconds) || undefined;
  if (type === "text") return sock.sendMessage(jid, { text }, opts);
  if (type === "ptt") return sock.sendMessage(jid, { audio: fs.readFileSync(file), ptt: true, mimetype: "audio/ogg; codecs=opus", seconds: secs }, opts);
  if (type === "audio") return sock.sendMessage(jid, { audio: { url: file }, ptt: false, mimetype: mime || "audio/mpeg", seconds: secs }, opts);
  if (type === "sticker") return sock.sendMessage(jid, { sticker: { url: file } }, opts);
  if (type === "gif") return sock.sendMessage(jid, { video: { url: file }, gifPlayback: true, mimetype: "video/mp4", ...caption }, opts);
  if (type === "video") return sock.sendMessage(jid, { video: { url: file }, mimetype: mime, ...caption }, opts);
  if (type === "image") return sock.sendMessage(jid, { image: { url: file }, mimetype: mime, ...caption }, opts);
  return sock.sendMessage(jid, { document: { url: file }, fileName: name || "file", mimetype: mime || "application/octet-stream", ...caption }, opts);
}
const KEY_OK = /^[A-Za-z0-9_.:-]{1,128}$/, JID_OK = /^[\w.:-]+@[\w.]+$/;
// group messages of others: the wire key needs the participant in the group's own addressing (lid or phone number)
async function participantOf(jid, p) {
  if (!jid.endsWith("@g.us") || !p) return undefined;
  try {
    const meta = await sock.groupMetadata(jid);
    const m = meta.participants.find(x => x.id === p || x.phoneNumber === p || x.lid === p);
    return m ? m.id : p;
  } catch { return p; }
}
async function msgKey(r) {
  return { remoteJid: r.jid, id: r.key_id, fromMe: !!r.from_me, participant: r.from_me ? undefined : await participantOf(r.jid, r.participant) };
}
async function deleteMsg(r) {
  const key = await msgKey(r);
  if (r.scope === "everyone") {
    await sock.sendMessage(r.jid, { delete: key });
    W({ op: "revoke", chat_jid: r.jid, key_id: r.key_id, from_me: key.fromMe ? 1 : 0, ts: now(), by: "me", source: "live" });
  } else {
    await sock.chatModify({ deleteForMe: { deleteMedia: false, key, timestamp: r.ts } }, r.jid);
    W({ op: "dmark", chat_jid: r.jid, key_id: r.key_id, from_me: key.fromMe ? 1 : 0 });
  }
}
// App -> phone: app-state markChatAsRead (chatModify) with the chat's latest message. Never readMessages (that sends read receipts).
function readPatch(key, ts) { return { markRead: true, lastMessages: [{ key, messageTimestamp: ts }] }; }
async function readChat(r) {
  await sock.chatModify(readPatch(await msgKey(r), r.ts), r.jid);
}
async function reactMsg(r) {
  const key = await msgKey(r);
  const sent = await sock.sendMessage(r.jid, { react: { text: r.emoji, key } });
  W({ op: "react", chat_jid: r.jid, key_id: r.key_id, from_me: key.fromMe ? 1 : 0, reactor: "me", emoji: r.emoji, ts: now() });
  return sent?.key?.id;
}
async function sendApi(req, res) {
  const respond = (code, obj) => { res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" }); res.end(JSON.stringify(obj)); };
  if (!tokenOk(req.headers["x-wa-token"])) return respond(401, { ok: false, error: "bad token" });
  const route = `${req.method} ${new URL(req.url, "http://127.0.0.1").pathname}`;
  if (route === "GET /health") return respond(200, { ok: true, connected: status.state === "connected" });
  if (!["POST /send", "POST /delete", "POST /react", "POST /read"].includes(route)) return respond(404, { ok: false, error: "not found" });
  let r;
  try { r = JSON.parse(await readBody(req, 1 << 20)); } catch (e) { return respond(400, { ok: false, error: e.message === "body too large" ? e.message : "bad JSON" }); }
  if (!r || typeof r !== "object") return respond(400, { ok: false, error: "bad JSON" });
  if (typeof r.jid !== "string" || !JID_OK.test(r.jid)) return respond(400, { ok: false, error: "bad jid" });
  if (route !== "POST /send") {  // delete / react: strict shape, then act on the message key
    const bad = e => respond(400, { ok: false, error: e });
    if (typeof r.key_id !== "string" || !KEY_OK.test(r.key_id)) return bad("bad key_id");
    if (typeof r.from_me !== "boolean") return bad("bad from_me");
    if (r.participant != null && (typeof r.participant !== "string" || !JID_OK.test(r.participant))) return bad("bad participant");
    const del = route === "POST /delete", rd = route === "POST /read";
    if ((del || rd) && !(Number.isSafeInteger(r.ts) && r.ts > 0)) return bad("bad ts");
    if (del && r.scope !== "me" && r.scope !== "everyone") return bad("bad scope");
    if (!del && !rd && (typeof r.emoji !== "string" || r.emoji.length > 32)) return bad("bad emoji");
    if (!sock || status.state !== "connected") return respond(503, { ok: false, error: "not connected" });
    try {
      const id = rd ? await readChat(r) : del ? await deleteMsg(r) : await reactMsg(r);
      log(`${rd ? "read" : del ? "delete " + r.scope : "react"} chat=${r.jid} key=${r.key_id}`);
      return respond(200, del || rd ? { ok: true } : { ok: true, key_id: id || null });
    } catch (e) {
      const error = String(e?.message || e).slice(0, 200);
      log(`${rd ? "read" : del ? "delete" : "react"} chat=${r.jid} failed: ${error}`);
      return respond(502, { ok: false, error });
    }
  }
  const { type, text, file, name, mime, seconds, reply: quote } = r;
  const { jid } = r;
  if (!SEND_TYPES.has(type)) return respond(400, { ok: false, error: "bad type" });
  if (type === "text" && (typeof text !== "string" || !text.trim() || text.length > 65536)) return respond(400, { ok: false, error: "bad text" });
  const abs = type === "text" ? null : outboxFile(file);
  if (type !== "text" && !abs) return respond(400, { ok: false, error: "file must be inside state/outbox" });
  if (quote && (typeof quote.key_id !== "string" || quote.key_id.length > 128)) return respond(400, { ok: false, error: "bad reply" });
  if (!sock || status.state !== "connected") return respond(503, { ok: false, error: "not connected" });
  try {
    const sent = await sendOne({ jid, type, text, file: abs, name: String(name || "").slice(0, 255),
      mime: mime ? String(mime).slice(0, 255) : undefined, seconds, reply: quote });
    const id = sent?.key?.id;
    if (!id) throw new Error("no message id");
    log(`send ${type} chat=${jid} key=${id}`);
    respond(200, { ok: true, key_id: id });
  } catch (e) {
    const error = String(e?.message || e).slice(0, 200);
    log(`send ${type} chat=${jid} failed: ${error}`);
    respond(502, { ok: false, error });
  }
}
const SYNC_PORT = Number(CONF.WA_SYNC_PORT) || 8778;
http.createServer(sendApi).on("error", e => log("send API:", e.message))
  .listen(SYNC_PORT, "127.0.0.1", () => log(`send API on 127.0.0.1:${SYNC_PORT}`));

startWriter();
heartbeat = setInterval(() => { if (!shuttingDown) setStatus(status.state, status.msg); }, 30000);
connect();
