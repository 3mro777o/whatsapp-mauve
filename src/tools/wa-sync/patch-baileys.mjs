// Baileys unwraps deviceSentMessage (our own messages sent from the phone) and drops the outer
// messageContextInfo, losing messageSecret -> edits of our own messages can't be decrypted. Keep it.
import fs from "node:fs";
const f = new URL("./node_modules/@whiskeysockets/baileys/lib/Utils/decode-wa-message.js", import.meta.url);
const old = "msg = msg.deviceSentMessage?.message || msg;";
const neu = "if (msg.deviceSentMessage?.message) { const o = msg; msg = o.deviceSentMessage.message; if (!msg.messageContextInfo?.messageSecret && o.messageContextInfo) msg.messageContextInfo = o.messageContextInfo; }";
const s = fs.readFileSync(f, "utf8");
if (s.includes(old)) { fs.writeFileSync(f, s.replace(old, neu)); console.log("baileys patched"); }
else if (!s.includes(neu)) { console.error("baileys patch: anchor not found"); process.exit(1); }
