/**
 * whatsapp_bridge.js — Baileys ↔ nbchat HTTP bridge
 */

import makeWASocket, {
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
} from "@whiskeysockets/baileys";
import fetch from "node-fetch";
import path from "path";
import os from "os";
import { Boom } from "@hapi/boom";
import pino from "pino";                     // <-- Added pino
import qrcode from "qrcode-terminal";        // <-- Added qrcode-terminal

const PYTHON_PORT = process.env.WA_PORT || "8764";
const PYTHON_URL  = `http://127.0.0.1:${PYTHON_PORT}`;
const CREDS_DIR   = process.env.WA_CREDS
    || path.join(os.homedir(), ".nbchat", "wa-creds");
const ALLOWED_FROM = (process.env.WA_ALLOW || "")
    .split(",").map(s => s.trim()).filter(Boolean);

const CHUNK_SIZE   = 3800;
const REQUEST_TIMEOUT_MS = 180_000;

// ── Text chunking ─────────────────────────────────────────────────────────

function chunkText(text, maxLen = CHUNK_SIZE) {
    if (text.length <= maxLen) return [text];
    const chunks = [];
    let rest = text;
    while (rest.length > maxLen) {
        let cut = rest.lastIndexOf("\n\n", maxLen);
        if (cut < maxLen * 0.5) cut = rest.lastIndexOf("\n",  maxLen);
        if (cut < maxLen * 0.5) cut = rest.lastIndexOf(". ",  maxLen);
        if (cut < 0) cut = maxLen;
        chunks.push(rest.slice(0, cut).trimEnd());
        rest = rest.slice(cut).trimStart();
    }
    if (rest) chunks.push(rest);
    return chunks;
}

// ── Connection ────────────────────────────────────────────────────────────

async function start() {
    const { state, saveCreds } = await useMultiFileAuthState(CREDS_DIR);
    const { version } = await fetchLatestBaileysVersion();

    const sock = makeWASocket({
        version,
        auth: state,
        // <-- Removed the deprecated printQRInTerminal
        // Provide a proper Pino logger to prevent crashes on internal errors
        logger: pino({ level: "silent" }), 
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect, qr } = update;

        // <-- Manually handle and print the QR code
        if (qr) {
            console.log("[bridge] Scan this QR code with WhatsApp:");
            qrcode.generate(qr, { small: true });
        }

        if (connection === "close") {
            const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
            const shouldReconnect = reason !== DisconnectReason.loggedOut;
            console.log(`[bridge] connection closed (${reason}), reconnect=${shouldReconnect}`);
            if (shouldReconnect) start();
        } else if (connection === "open") {
            console.log("[bridge] connected to WhatsApp");
        }
    });

    sock.ev.on("messages.upsert", async ({ messages, type }) => {
        if (type !== "notify") return;

        for (const msg of messages) {
            if (!msg.message) continue;

            const jid  = msg.key.remoteJid;
            // We allow messages 'fromMe' ONLY if the JID is your own (the "Message Yourself" chat).
            // This prevents the bot from responding to messages you send to OTHER people.
            const isMe = msg.key.fromMe;
            const isSelfChat = jid === sock.user.id.split(':')[0] + '@s.whatsapp.net';
            // If it's from you, but NOT in your private "Me" chat, skip it.
            if (isMe && !isSelfChat) continue;
            const text = msg.message.conversation
                      || msg.message.extendedTextMessage?.text
                      || "";
            
            if (!text.trim()) continue;

            if (!text.startsWith('!')) continue;

            const constext = text.slice(1).trim();

            if (ALLOWED_FROM.length) {
                const number = jid.split("@")[0].replace(/[^0-9+]/g, "");
                if (!ALLOWED_FROM.includes(number)) {
                    console.log(`[bridge] ignored message from unlisted sender: ${number}`);
                    continue;
                }
            }

            try {
                await sock.sendMessage(jid, { react: { text: "👀", key: msg.key } });
            } catch (_) { /* non-fatal */ }

            console.log(`[bridge] → python  jid=${jid}  len=${constext.length}`);

            let reply = "";
            try {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
                const res = await fetch(`${PYTHON_URL}/message`, {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify({ jid, constext }),
                    signal:  controller.signal,
                });
                clearTimeout(timer);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                reply = (await res.json()).reply || "";
            } catch (err) {
                console.error(`[bridge] python error: ${err.message}`);
                reply = "Sorry, I encountered an error processing your message.";
            }

            console.log(`[bridge] ← python  len=${reply.length}`);

            for (const chunk of chunkText(reply)) {
                try {
                    await sock.sendMessage(jid, { text: chunk });
                } catch (err) {
                    console.error(`[bridge] send error: ${err.message}`);
                }
            }

            try {
                await sock.sendMessage(jid, { react: { text: "", key: msg.key } });
            } catch (_) { /* non-fatal */ }
        }
    });
}

start().catch(err => {
    console.error("[bridge] fatal:", err);
    process.exit(1);
});