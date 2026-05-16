'use strict';

/**
 * SabiWiFi WhatsApp sidecar service.
 *
 * Manages one Baileys WhatsApp Web session per reseller slug.
 * Sessions persist to disk at ./sessions/<slug>/.
 *
 * Internal HTTP API (127.0.0.1:3001):
 *   POST /sessions/:slug/connect      Start session / refresh QR
 *   GET  /sessions/:slug/status       { status, phone, qr }
 *   POST /sessions/:slug/disconnect   Log out + delete auth files
 *   POST /send                        { slug, to, message } → queued
 *   GET  /health                      liveness
 *
 * On session events, POSTs back to Django:
 *   POST DJANGO_WEBHOOK_URL { event, slug, phone?, error? }
 *
 * On message send result:
 *   POST DJANGO_WEBHOOK_URL { event:'sent'|'failed', log_id, slug, error? }
 *
 * On inbound customer messages (1:1 only, groups ignored):
 *   POST DJANGO_INBOUND_URL { event:'message_received', slug, from_jid, from_phone,
 *                             body, attachments, external_message_id, timestamp }
 */

const express = require('express');
const path    = require('path');
const fs      = require('fs');
const axios   = require('axios');
const QRCode  = require('qrcode');

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require('@whiskeysockets/baileys');

const pino = require('pino');
const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

const PORT             = parseInt(process.env.WA_PORT || '3001', 10);
const SESSIONS_DIR     = process.env.SESSIONS_DIR || path.join(__dirname, 'sessions');
const DJANGO_WEBHOOK   = process.env.DJANGO_WEBHOOK_URL || 'http://127.0.0.1:8000/api/notifications/wa-webhook/';
const DJANGO_INBOUND   = process.env.DJANGO_INBOUND_URL || 'http://127.0.0.1:8000/api/conversations/inbound-wa/';
const DJANGO_API_KEY   = process.env.WA_API_KEY || '';
// Milliseconds between sends per session (rate limit)
const SEND_DELAY_MS    = parseInt(process.env.SEND_DELAY_MS || '4000', 10);

fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// In-memory session registry
// sessions[slug] = { socket, status, phone, qrBase64, sendQueue, processing }
const sessions = {};

// ---------------------------------------------------------------------------
// Django webhook helper
// ---------------------------------------------------------------------------
// Loopback POSTs go straight to gunicorn (bypassing nginx). Django will
// 301-redirect to https:// unless we tell it the request is already secure
// via X-Forwarded-Proto, since SECURE_SSL_REDIRECT is on in prod.
function djangoHeaders() {
  const headers = {
    'Content-Type': 'application/json',
    'X-Forwarded-Proto': 'https',
  };
  if (DJANGO_API_KEY) headers['X-WA-API-Key'] = DJANGO_API_KEY;
  return headers;
}

async function djangoWebhook(payload) {
  try {
    await axios.post(DJANGO_WEBHOOK, payload, { headers: djangoHeaders(), timeout: 8000 });
  } catch (err) {
    logger.warn({ slug: payload.slug, event: payload.event, err: err.message }, 'Django webhook failed');
  }
}

async function djangoInbound(payload) {
  try {
    await axios.post(DJANGO_INBOUND, payload, { headers: djangoHeaders(), timeout: 8000 });
  } catch (err) {
    logger.warn({ slug: payload.slug, err: err.message }, 'Django inbound webhook failed');
  }
}

// Extract a plain-text body + attachment descriptors from a Baileys message.
// Media is NOT downloaded here — we only record type + mimetype so the
// operator UI can show "customer sent an image" pending Phase 2 media handling.
function extractMessage(msg) {
  const m = msg.message;
  if (!m) return { body: '', attachments: [] };

  let body = '';
  const attachments = [];

  if (m.conversation) {
    body = m.conversation;
  } else if (m.extendedTextMessage?.text) {
    body = m.extendedTextMessage.text;
  } else if (m.imageMessage) {
    body = m.imageMessage.caption || '';
    attachments.push({ kind: 'image', mimetype: m.imageMessage.mimetype || 'image/jpeg' });
  } else if (m.videoMessage) {
    body = m.videoMessage.caption || '';
    attachments.push({ kind: 'video', mimetype: m.videoMessage.mimetype || 'video/mp4' });
  } else if (m.audioMessage) {
    attachments.push({ kind: 'audio', mimetype: m.audioMessage.mimetype || 'audio/ogg', seconds: m.audioMessage.seconds || null });
  } else if (m.documentMessage) {
    body = m.documentMessage.caption || m.documentMessage.fileName || '';
    attachments.push({ kind: 'document', mimetype: m.documentMessage.mimetype || 'application/octet-stream', filename: m.documentMessage.fileName || null });
  } else if (m.stickerMessage) {
    attachments.push({ kind: 'sticker', mimetype: m.stickerMessage.mimetype || 'image/webp' });
  } else if (m.locationMessage) {
    const lat = m.locationMessage.degreesLatitude;
    const lon = m.locationMessage.degreesLongitude;
    body = `[location] ${lat}, ${lon}`;
    attachments.push({ kind: 'location', lat, lon });
  } else if (m.contactMessage) {
    body = m.contactMessage.displayName || '';
    attachments.push({ kind: 'contact', vcard: m.contactMessage.vcard || null });
  } else {
    // Unknown type — record it so the agent sees something rather than silent drop
    const keys = Object.keys(m);
    attachments.push({ kind: 'unknown', types: keys });
  }

  return { body, attachments };
}

// ---------------------------------------------------------------------------
// Send queue per session
// ---------------------------------------------------------------------------
function enqueueSend(slug, to, message, logId) {
  const sess = sessions[slug];
  if (!sess) return;
  sess.sendQueue.push({ to, message, logId });
  if (!sess.processing) drainQueue(slug);
}

async function drainQueue(slug) {
  const sess = sessions[slug];
  if (!sess || sess.processing) return;
  sess.processing = true;

  while (sess.sendQueue.length > 0) {
    if (sess.status !== 'connected') break;
    const { to, message, logId } = sess.sendQueue.shift();
    const jid = to.replace(/\D/g, '') + '@s.whatsapp.net';

    try {
      await sess.socket.sendMessage(jid, { text: message });
      logger.info({ slug, to, logId }, 'WA message sent');
      await djangoWebhook({ event: 'sent', slug, log_id: logId });
    } catch (err) {
      logger.error({ slug, to, logId, err: err.message }, 'WA send failed');
      await djangoWebhook({ event: 'failed', slug, log_id: logId, error: err.message });
    }

    // Rate limit — wait between sends
    await new Promise(r => setTimeout(r, SEND_DELAY_MS));
  }

  sess.processing = false;
}

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------
async function startSession(slug) {
  if (sessions[slug] && sessions[slug].status === 'connected') {
    logger.info({ slug }, 'Session already connected');
    return;
  }

  // Tear down any existing socket cleanly
  if (sessions[slug]?.socket) {
    try { sessions[slug].socket.end(undefined); } catch (_) {}
  }

  sessions[slug] = {
    socket: null,
    status: 'connecting',
    phone: '',
    qrBase64: null,
    sendQueue: sessions[slug]?.sendQueue || [],
    processing: false,
  };

  const sessDir = path.join(SESSIONS_DIR, slug);
  fs.mkdirSync(sessDir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(sessDir);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, pino({ level: 'silent' })),
    },
    printQRInTerminal: false,
    logger: pino({ level: 'silent' }),
    browser: ['SabiWiFi', 'Chrome', '124.0.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
  });

  sessions[slug].socket = sock;

  sock.ev.on('creds.update', saveCreds);

  // Inbound message handler — forwards 1:1 customer messages to Django.
  // Filters:
  //  - Skip own messages (fromMe)
  //  - Skip group chats (@g.us) — only 1:1 customer conversations
  //  - Skip historical sync (type !== 'notify')
  //  - Skip messages with no content at all
  sock.ev.on('messages.upsert', async (event) => {
    logger.info({ slug, type: event.type, count: (event.messages || []).length },
                'messages.upsert received');
    if (event.type !== 'notify') return;
    for (const msg of (event.messages || [])) {
      try {
        if (!msg.key) { logger.debug({ slug }, 'skip: no key'); continue; }
        if (msg.key.fromMe) { logger.debug({ slug, id: msg.key.id }, 'skip: fromMe'); continue; }
        const jid = msg.key.remoteJid || '';

        // 1:1 customer chats arrive as either @s.whatsapp.net (classic) or
        // @lid (WhatsApp's newer pseudonymous identifier). Skip everything
        // else: groups (@g.us), status updates (status@broadcast), broadcast
        // lists (@broadcast), newsletters (@newsletter).
        const isClassic = jid.endsWith('@s.whatsapp.net');
        const isLid     = jid.endsWith('@lid');
        if (!isClassic && !isLid) {
          logger.info({ slug, jid }, 'skip: non-1to1 jid');
          continue;
        }

        const { body, attachments } = extractMessage(msg);
        if (!body && attachments.length === 0) {
          logger.info({ slug, jid, id: msg.key.id, msgKeys: Object.keys(msg.message || {}) },
                      'skip: empty body+attachments');
          continue;
        }

        // Resolve the sender's real phone number.
        //  - Classic @s.whatsapp.net JID: phone is the local part.
        //  - @lid JID: the LID is opaque; Baileys exposes the phone as
        //    msg.key.senderPn (a phone-number JID like `234…@s.whatsapp.net`).
        //    Fall back to participantPn (group context) and finally to the
        //    LID itself so the conversation still lands somewhere.
        let fromPhoneJid = '';
        if (isClassic) {
          fromPhoneJid = jid;
        } else {
          fromPhoneJid = msg.key.senderPn || msg.key.participantPn || '';
        }
        const fromPhone = fromPhoneJid
          ? fromPhoneJid.split('@')[0]
          : jid.split('@')[0];  // last-resort: keep the LID as contact id

        logger.info({ slug, jid, fromPhone, id: msg.key.id, bodyLen: body.length, lid: isLid },
                    'forwarding inbound to Django');
        await djangoInbound({
          event: 'message_received',
          slug,
          // Conversation key: prefer the phone JID so threads don't fragment
          // when a sender's LID changes. Falls back to the LID if no PN.
          from_jid: fromPhoneJid || jid,
          from_phone: fromPhone,
          display_name: msg.pushName || '',
          body,
          attachments,
          external_message_id: msg.key.id,
          timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
        });
      } catch (err) {
        logger.warn({ slug, err: err.message }, 'Inbound forward failed for a message');
      }
    }
  });

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        sessions[slug].qrBase64 = await QRCode.toDataURL(qr);
        logger.info({ slug }, 'QR code generated');
      } catch (err) {
        logger.error({ slug, err: err.message }, 'QR generation failed');
      }
    }

    if (connection === 'open') {
      const phone = sock.user?.id?.split(':')[0] || '';
      sessions[slug].status = 'connected';
      sessions[slug].phone = phone;
      sessions[slug].qrBase64 = null;
      logger.info({ slug, phone }, 'WhatsApp connected');
      await djangoWebhook({ event: 'connected', slug, phone });
      // Drain any queued messages
      drainQueue(slug);
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;

      logger.info({ slug, code, loggedOut }, 'WhatsApp disconnected');

      if (loggedOut) {
        // Permanent logout — delete auth files, update state
        sessions[slug].status = 'disconnected';
        sessions[slug].qrBase64 = null;
        sessions[slug].phone = '';
        try { fs.rmSync(path.join(SESSIONS_DIR, slug), { recursive: true, force: true }); } catch (_) {}
        await djangoWebhook({ event: 'disconnected', slug, reason: 'logged_out' });
      } else {
        // Transient disconnect — reconnect automatically
        sessions[slug].status = 'connecting';
        setTimeout(() => startSession(slug), 5000);
      }
    }
  });
}

async function disconnectSession(slug) {
  const sess = sessions[slug];
  if (!sess) return;
  try {
    await sess.socket?.logout();
  } catch (_) {}
  sess.status = 'disconnected';
  sess.qrBase64 = null;
  sess.phone = '';
  try { fs.rmSync(path.join(SESSIONS_DIR, slug), { recursive: true, force: true }); } catch (_) {}
  delete sessions[slug];
  await djangoWebhook({ event: 'disconnected', slug, reason: 'manual' });
}

// Restore persisted sessions on startup
async function restorePersistedSessions() {
  if (!fs.existsSync(SESSIONS_DIR)) return;
  const slugs = fs.readdirSync(SESSIONS_DIR).filter(d =>
    fs.statSync(path.join(SESSIONS_DIR, d)).isDirectory()
  );
  for (const slug of slugs) {
    logger.info({ slug }, 'Restoring persisted session');
    await startSession(slug);
    // Stagger restores to avoid hammering WA servers
    await new Promise(r => setTimeout(r, 2000));
  }
}

// ---------------------------------------------------------------------------
// Express API
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json());

// Liveness
app.get('/health', (_req, res) => res.json({ ok: true, sessions: Object.keys(sessions).length }));

// Start / refresh session
app.post('/sessions/:slug/connect', async (req, res) => {
  const { slug } = req.params;
  try {
    await startSession(slug);
    res.json({ ok: true, status: sessions[slug]?.status || 'connecting' });
  } catch (err) {
    logger.error({ slug, err: err.message }, 'connect failed');
    res.status(500).json({ error: err.message });
  }
});

// Status + current QR
app.get('/sessions/:slug/status', (req, res) => {
  const { slug } = req.params;
  const sess = sessions[slug];
  if (!sess) return res.json({ status: 'disconnected', phone: '', qr: null });
  res.json({ status: sess.status, phone: sess.phone, qr: sess.qrBase64 });
});

// Disconnect
app.post('/sessions/:slug/disconnect', async (req, res) => {
  const { slug } = req.params;
  try {
    await disconnectSession(slug);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Send message
app.post('/send', (req, res) => {
  const { slug, to, message, log_id } = req.body;
  if (!slug || !to || !message) {
    return res.status(400).json({ error: 'slug, to, message required' });
  }
  const sess = sessions[slug];
  if (!sess || sess.status !== 'connected') {
    return res.status(503).json({ error: 'Session not connected', status: sess?.status || 'disconnected' });
  }
  enqueueSend(slug, to, message, log_id);
  res.json({ queued: true, queue_length: sess.sendQueue.length });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
app.listen(PORT, '127.0.0.1', async () => {
  logger.info(`WA service listening on 127.0.0.1:${PORT}`);
  await restorePersistedSessions();
});
