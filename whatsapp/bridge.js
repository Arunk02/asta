/**
 * Asta WhatsApp bridge (Baileys, unofficial WhatsApp Web protocol).
 *
 * - First run: scan the QR with WhatsApp on your phone (Linked devices).
 * - Talk to Asta in your own "Message yourself" chat (or set WA_ALLOWED_JID).
 * - Asta replies in the same chat; notifications are pushed here too.
 *
 * HTTP (127.0.0.1:8323): GET /status · POST /send {text} · POST /config
 *   {enabled, allowed_jid} (Bearer ASTA_TOKEN) — config persists in config.json
 *
 * NOTE: unofficial automation — use your own account, low volume. Small ban risk.
 */

import fs from "fs";
import http from "http";
import path from "path";
import { fileURLToPath } from "url";
import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import QRCode from "qrcode";
import pino from "pino";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Minimal .env loader (shares the asta .env)
const envPath = path.join(__dirname, "..", ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf8").split("\n")) {
    const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)$/);
    if (m && !(m[1] in process.env)) process.env[m[1]] = m[2];
  }
}

const TOKEN = process.env.ASTA_TOKEN || "";
const ASTA_URL = process.env.ASTA_URL || "http://127.0.0.1:8321";
const PORT = Number(process.env.WA_BRIDGE_PORT || 8323);
const BOT_MARK = "🤖 ";

let sock = null;
let paired = false;
let selfJid = null;
let selfLid = null; // account's privacy LID — self-chat messages arrive addressed to this
let lastQr = null; // latest pairing QR string (null once paired)
let lastQrPng = null; // same QR as a data-URL PNG for the web UI
const sentByMe = new Set(); // message ids the bridge sent (loop guard)

// WhatsApp re-delivers the same message id — once as a live "notify" and again
// when a linked device syncs the chat. In a group that happens routinely, and
// the second copy reached Asta as a SEPARATE message: it landed mid-turn, got
// treated as a follow-up to itself, and answered "adding that to what I'm
// doing" instead of the actual reply. Dedupe on message id, bounded.
const seenIds = new Set();
function alreadyHandled(id) {
  if (!id) return false;
  if (seenIds.has(id)) return true;
  seenIds.add(id);
  if (seenIds.size > 500) for (const k of seenIds) { seenIds.delete(k); if (seenIds.size <= 400) break; }
  return false;
}

// Runtime config, editable from the Asta UI. File wins over .env defaults.
const CONFIG_PATH = path.join(__dirname, "config.json");
let config = { enabled: true, allowed_jid: process.env.WA_ALLOWED_JID || "" };
try {
  if (fs.existsSync(CONFIG_PATH)) config = { ...config, ...JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")) };
} catch {}
function saveConfig() {
  try { fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2)); } catch {}
}

function targetJid() {
  return config.allowed_jid || selfJid;
}

// WhatsApp surfaces the same chat under phone-number (@s.whatsapp.net) or
// privacy LID (@lid) addressing depending on device/route — match on either.
function normJid(j) {
  return String(j || "").split("@")[0].split(":")[0];
}
function isAllowedChat(jid) {
  const allowed = targetJid();
  if (!jid || !allowed) return false;
  if (jid === allowed || normJid(jid) === normJid(allowed)) return true;
  // self-chat: messages can arrive addressed to the account's LID
  if (normJid(allowed) === normJid(selfJid) && selfLid && normJid(jid) === normJid(selfLid)) return true;
  return false;
}

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(__dirname, "auth"));
  const { version } = await fetchLatestBaileysVersion();
  sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
    browser: ["Asta", "Chrome", "1.0"],
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (u) => {
    if (u.qr) {
      lastQr = u.qr;
      QRCode.toDataURL(u.qr, { width: 280 }).then((d) => (lastQrPng = d)).catch(() => {});
      console.log("\nScan this QR in WhatsApp → Settings → Linked devices:\n");
      qrcode.generate(u.qr, { small: true });
    }
    if (u.connection === "open") {
      paired = true;
      lastQr = null;
      lastQrPng = null;
      selfJid = sock.user?.id?.replace(/:\d+@/, "@");
      // lid isn't always on sock.user — fall back to the saved creds
      let lidRaw = sock.user?.lid || null;
      if (!lidRaw) {
        try {
          lidRaw = JSON.parse(fs.readFileSync(path.join(__dirname, "auth", "creds.json"), "utf8"))?.me?.lid || null;
        } catch {}
      }
      selfLid = lidRaw ? lidRaw.replace(/:\d+@/, "@") : null;
      console.log("WhatsApp connected as", selfJid, "lid:", selfLid);
    }
    if (u.connection === "close") {
      paired = false;
      const code = u.lastDisconnect?.error?.output?.statusCode;
      if (code !== DisconnectReason.loggedOut) {
        console.log(`Connection closed (code ${code}), reconnecting…`);
        setTimeout(connect, 3000);
      } else {
        console.log("Logged out — delete whatsapp/auth/ and re-pair.");
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      if (!config.enabled) continue;
      const jid = msg.key.remoteJid;
      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        "";
      if (text && !isAllowedChat(jid)) {
        console.log(`dropped (chat not allowed): jid=${jid} fromMe=${msg.key.fromMe} text="${text.slice(0, 40)}"`);
      }
      if (!isAllowedChat(jid)) continue;
      if (sentByMe.has(msg.key.id)) continue;
      if (!text || text.startsWith(BOT_MARK)) continue;
      if (alreadyHandled(msg.key.id)) {
        console.log(`dropped (duplicate delivery): id=${msg.key.id} text="${text.slice(0, 40)}"`);
        continue;
      }
      console.log("→ asta:", text.slice(0, 80));
      try {
        const r = await fetch(`${ASTA_URL}/api/wa/incoming`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` },
          body: JSON.stringify({ text }),
        });
        const data = await r.json();
        if (data.reply) await send(data.reply);
      } catch (e) {
        await send("(bridge error: " + e.message + ")");
      }
    }
  });
}

async function send(text) {
  let jid = targetJid();
  if (!config.enabled || !sock || !paired || !jid) return false;
  // lid-migrated accounts: the self-chat must be addressed by LID, else the
  // phone can't decrypt and shows "waiting for this message"
  if (selfLid && normJid(jid) === normJid(selfJid)) jid = selfLid;
  const res = await sock.sendMessage(jid, { text: BOT_MARK + text });
  if (res?.key?.id) sentByMe.add(res.key.id);
  if (sentByMe.size > 500) sentByMe.clear();
  return true;
}

http
  .createServer(async (req, res) => {
    const auth = req.headers.authorization || "";
    if (req.method === "GET" && req.url === "/status") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        up: true, paired, jid: targetJid() || null,
        enabled: config.enabled, allowed_jid: config.allowed_jid,
        qr: paired ? null : lastQrPng,
      }));
      return;
    }
    if (req.method === "POST" && req.url === "/create-group") {
      // Creates a WhatsApp group with just you in it, named after the assistant —
      // a dedicated chat so your "Message yourself" stays clean. Locks the bridge to it.
      if (TOKEN && auth !== `Bearer ${TOKEN}`) {
        res.writeHead(401); res.end(); return;
      }
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", async () => {
        try {
          if (!sock || !paired) throw new Error("not paired");
          const name = (JSON.parse(body || "{}").name || "Asta").slice(0, 25);
          const g = await sock.groupCreate(name, []);
          config.allowed_jid = g.id;
          saveConfig();
          await send(`This is your dedicated ${name} chat. Talk to me here.`);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true, jid: g.id, name }));
        } catch (e) {
          res.writeHead(500); res.end(JSON.stringify({ ok: false, error: e.message }));
        }
      });
      return;
    }
    if (req.method === "POST" && req.url === "/config") {
      if (TOKEN && auth !== `Bearer ${TOKEN}`) {
        res.writeHead(401); res.end(); return;
      }
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        try {
          const b = JSON.parse(body);
          if (typeof b.enabled === "boolean") config.enabled = b.enabled;
          if (typeof b.allowed_jid === "string") config.allowed_jid = b.allowed_jid.trim();
          saveConfig();
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true, ...config }));
        } catch (e) {
          res.writeHead(400); res.end(JSON.stringify({ error: e.message }));
        }
      });
      return;
    }
    if (req.method === "POST" && req.url === "/send") {
      if (TOKEN && auth !== `Bearer ${TOKEN}`) {
        res.writeHead(401); res.end(); return;
      }
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", async () => {
        try {
          const ok = await send(JSON.parse(body).text || "");
          res.writeHead(ok ? 200 : 503, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok }));
        } catch (e) {
          res.writeHead(500); res.end(JSON.stringify({ error: e.message }));
        }
      });
      return;
    }
    res.writeHead(404); res.end();
  })
  .listen(PORT, "127.0.0.1", () => console.log(`Bridge HTTP on 127.0.0.1:${PORT}`));

connect();
