#!/usr/bin/env node
// Link as a WhatsApp "linked device" (scan QR once) and download profile pictures
// for every chat in archive/web/data/chats.json, then hand them to the viewer API.
//
//   wa-fetch-avatars            fetch missing pictures
//   wa-fetch-avatars --all      re-fetch everything (refresh changed pictures)
//   wa-fetch-avatars --logout   unlink this device and delete the session
import makeWASocket, { useMultiFileAuthState, DisconnectReason, Browsers } from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import pino from "pino";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../../..");  // src/tools/avatar-fetcher -> repo root
const AUTH = path.join(HERE, "auth");
const INBOX = path.join(ROOT, "avatars-inbox");
const API = process.env.WA_API || "http://127.0.0.1:8777";
// the viewer's PIN lock lets this tool through with the local token that wa-archive keeps in state/api-token
const TOKEN = (() => { try { return fs.readFileSync(path.join(ROOT, "state/api-token"), "utf8").trim(); } catch { return ""; } })();
const api = (p, o = {}) => fetch(API + p, { ...o, headers: { ...o.headers, "X-WA-Token": TOKEN } });
const args = new Set(process.argv.slice(2));
const sleep = ms => new Promise(r => setTimeout(r, ms));

// newsletters/channels don't answer profile-picture queries (they just time out): skip them
// the live sync service uses the same linked-device session: never run both at once
try {
  const st = JSON.parse(fs.readFileSync(path.join(ROOT, "state/status.json"), "utf8"));
  if (st.state === "connected" && Date.now() / 1000 - st.last < 120) {
    console.log("wa-sync شغّال وعم يجيب الصور الناقصة تلقائياً. لتشغيل هالأداة وقّفه أول: systemctl --user stop wa-sync");
    process.exit(1);
  }
} catch {}
let allChats;
try { const r = await api("/data/chats.json"); if (!r.ok) throw new Error(String(r.status)); allChats = await r.json(); }
catch { allChats = JSON.parse(fs.readFileSync(path.join(ROOT, "archive/web/data/chats.json"), "utf8")); }
const chats = allChats.filter(c => c.j && c.j !== "status" && !(c.jid || "").endsWith("@newsletter"));

async function existingAvatars() {
  try { const r = await api("/api/avatars"); return r.ok ? await r.json() : null; } catch { return null; }
}

async function save(chat, buf) {
  try {
    const r = await api(`/api/avatar/${chat.id}`, { method: "POST", headers: { "Content-Type": "image/jpeg" }, body: buf });
    if (r.ok) return "api";
  } catch {}
  // server not running: drop into the inbox, named by number (wa-avatars imports it later)
  fs.mkdirSync(INBOX, { recursive: true });
  fs.writeFileSync(path.join(INBOX, `${chat.j}.jpg`), buf);
  return "inbox";
}

// Candidate jids to ask for: the chat's own jid, its LID<->phone alternative (from msgstore jid_map,
// or Baileys' own lid-mapping store), and the legacy j@s.whatsapp.net guess.
async function candidates(sock, c) {
  const out = [];
  const add = j => { if (j && !out.includes(j)) out.push(j); };
  add(c.jid || c.j + (c.g ? "@g.us" : "@s.whatsapp.net"));
  add(c.alt);
  const lm = sock.signalRepository?.lidMapping;
  try {
    if (lm && out[0].endsWith("@lid")) add(await lm.getPNForLID(out[0]));
    else if (lm && out[0].endsWith("@s.whatsapp.net")) add(await lm.getLIDForPN(out[0]));
  } catch {}
  return out;
}

// Baileys wraps IQ errors as Boom(500) with the real code in e.data and text in e.message
const statusOf = e => (typeof e?.data === "number" ? e.data : null) ?? e?.output?.statusCode ?? null;
const isHidden = e => [401, 403, 404].includes(statusOf(e))
  || /not-authorized|item-not-found|forbidden|not-acceptable/i.test(String(e?.message || e));

// returns "ok" | "none" | {error, code, rate}
async function fetchOne(sock, c) {
  let lastErr = null, hidden = false;
  for (const jid of await candidates(sock, c)) {
    try {
      const url = await sock.profilePictureUrl(jid, "image", 20000);
      if (!url) { hidden = true; continue; }
      const res = await fetch(url);
      if (!res.ok) throw Object.assign(new Error("download " + res.status), { data: res.status });
      await save(c, Buffer.from(await res.arrayBuffer()));
      return "ok";
    } catch (e) {
      const code = statusOf(e);
      if (isHidden(e)) { hidden = true; continue; }
      lastErr = { error: String(e?.message || e).slice(0, 120), code, rate: String(code) === "429" || /rate/i.test(String(e?.message)) };
      if (lastErr.rate) return lastErr;
    }
  }
  return hidden && !lastErr ? "none" : (lastErr || "none");
}

async function run(sock) {
  const have = args.has("--all") ? {} : (await existingAvatars()) || {};
  let todo = chats.filter(c => !have[c.id]);
  console.log(`\n✅ متصل. ${todo.length} محادثة للجلب (من ${chats.length}).\n`);
  let ok = 0, none = 0, errors = [];
  for (let pass = 1; pass <= 3 && todo.length; pass++) {
    if (pass > 1) {
      const wait = 20000 * (pass - 1);
      console.log(`\n↻ المحاولة ${pass}: ${todo.length} فشلت، استنّى ${wait / 1000}ث…`);
      await sleep(wait);
    }
    const retry = [];
    let delay = 350;
    for (const [i, c] of todo.entries()) {
      process.stdout.write(`\r[${pass}: ${i + 1}/${todo.length}] ✓${ok} ∅${none} ✗${retry.length}  `);
      const r = await fetchOne(sock, c);
      if (r === "ok") { ok++; delay = Math.max(350, delay * 0.8); }
      else if (r === "none") none++;
      else {
        retry.push(c); errors.push([c, r]);
        if (r.rate) { delay = Math.min(5000, delay * 2); await sleep(15000 * pass); }
      }
      await sleep(delay + Math.random() * 300);
    }
    todo = retry;
    if (todo.length) errors = errors.filter(([c]) => todo.includes(c));
    else errors = [];
  }
  const last = new Map(errors.map(([c, r]) => [c.id, r]));
  console.log(`\n\nتم: ${chats.length} محادثة · ${ok} صورة انحفظت · ${none} بدون صورة أو مخفية بالخصوصية · ${last.size} خطأ`);
  for (const [id, r] of last) console.log(`  ✗ #${id}: ${r.code || ""} ${r.error}`);
  if (fs.existsSync(INBOX) && fs.readdirSync(INBOX).some(f => f.endsWith(".jpg")))
    console.log("بعض الصور انحطت بـ avatars-inbox/ لأن العارض مش شغّال. شغّل wa-archive وبعدين wa-avatars.");
  console.log(`SUMMARY total=${chats.length} saved=${ok} hidden=${none} errors=${last.size}`);
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH);
  const sock = makeWASocket({
    auth: state, logger: pino({ level: "silent" }), browser: Browsers.ubuntu("WA Archive"),
    syncFullHistory: false, markOnlineOnConnect: false, shouldSyncHistoryMessage: () => false,
  });
  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      if (!process.stdout.isTTY || args.has("--no-qr")) {
        console.log("SESSION_EXPIRED: الجلسة منتهية، لازم تشغّل الأمر بترمنال وتمسح QR من جديد.");
        process.exit(2);
      }
      console.clear();
      console.log("امسح الكود من التلفون: واتساب ← الأجهزة المرتبطة ← ربط جهاز\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      if (args.has("--logout")) { await sock.logout(); fs.rmSync(AUTH, { recursive: true, force: true }); console.log("تم فك الربط."); process.exit(0); }
      await run(sock);
      sock.end(undefined);
      process.exit(0);
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        fs.rmSync(AUTH, { recursive: true, force: true });
        console.log("انفك الربط من التلفون. شغّل الأمر من جديد لتمسح QR.");
        process.exit(1);
      }
      start(); // restartRequired after pairing, or transient drop
    }
  });
}
start();
