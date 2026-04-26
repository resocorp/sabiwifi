// avr-llm-sabiwifi
//
// Drop-in replacement for avr-llm-openai / avr-llm-anthropic that forwards
// the AVR Core prompt to SabiWiFi Django's /api/ai/voice-turn/ endpoint.
// Django runs the LLM (via ResellerAIConfig) and returns the text to speak.
//
// AVR Core contract (verified against avr-llm-openai README):
//   POST /prompt-stream
//   body: { uuid, messages: [{role, content}, ...] }
//   response: text/event-stream of JSON objects { type, content }
//
// Phase 2 MVP: we call Django non-streaming (Django replies with one JSON
// body) and emit the result as a single SSE event. Phase 2.5 replaces this
// with true streaming once latency benchmarks demand it.

import express from 'express';

const PORT = parseInt(process.env.PORT || '6002', 10);
const DJANGO_URL = process.env.DJANGO_URL || 'https://app.sabiwifi.com';
const VOICE_API_KEY = process.env.VOICE_API_KEY || '';
const REQUEST_TIMEOUT_MS = parseInt(process.env.REQUEST_TIMEOUT_MS || '15000', 10);

// AVR Core routes each call to a specific LLM container based on the
// dialplan. For per-tenant prompt + tool routing we need the tenant_slug
// AND the current Asterisk call_id in every prompt-stream request.
// AVR Core doesn't currently pass these out of the box — the dialplan
// sets them as custom channel vars and the avr-core fork we ship passes
// them through as x-sabiwifi-* headers. For now we fall back to defaults
// from env so single-tenant pilots can boot without the fork.
const DEFAULT_TENANT_SLUG = process.env.DEFAULT_TENANT_SLUG || '';

const app = express();
app.use(express.json({ limit: '1mb' }));

// --- Health ---------------------------------------------------------------
app.get('/health', (_req, res) => {
  res.json({ ok: true, service: 'avr-llm-sabiwifi', upstream: DJANGO_URL });
});

// --- AVR prompt-stream ----------------------------------------------------
app.post('/prompt-stream', async (req, res) => {
  const { uuid: callId, messages = [] } = req.body || {};

  // Per-request header overrides (see note above). Tenant slug is the only
  // per-tenant routing signal we need — Django looks up everything else
  // (VoiceTenant, ResellerAIConfig, prompt overrides) from that.
  const tenantSlug =
    (req.headers['x-sabiwifi-tenant'] || DEFAULT_TENANT_SLUG || '').toString();
  const callerPhone = (req.headers['x-sabiwifi-caller'] || '').toString();

  if (!callId) {
    res.status(400).json({ error: 'uuid required' });
    return;
  }
  if (!tenantSlug) {
    res.status(400).json({
      error: 'tenant_slug missing — set X-Sabiwifi-Tenant header or DEFAULT_TENANT_SLUG env',
    });
    return;
  }

  const latest = messages.length ? messages[messages.length - 1] : null;
  const transcript = latest && latest.role === 'user' ? (latest.content || '') : '';

  // AVR Core's parser expects raw JSON per chunk (no `data: ` SSE prefix,
  // no end marker — connection close = end-of-stream). Confirmed by reading
  // avr-llm-openai source: it sets text/event-stream Content-Type but writes
  // bare JSON.stringify(obj) per delta. Following the same wire format.
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders?.();

  const sendChunk = (obj) => {
    res.write(JSON.stringify(obj));
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const djangoResp = await fetch(`${DJANGO_URL}/api/ai/voice-turn/`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Voice-API-Key': VOICE_API_KEY,
      },
      body: JSON.stringify({
        tenant_slug: tenantSlug,
        call_id: callId,
        transcript,
        history: messages.slice(0, -1),
        caller_phone: callerPhone,
      }),
      signal: controller.signal,
    });
    clearTimeout(timer);

    if (!djangoResp.ok) {
      const errBody = await djangoResp.text();
      console.error(`[bridge] django ${djangoResp.status}: ${errBody.slice(0, 300)}`);
      sendChunk({
        type: 'text',
        content: "I'm having trouble right now. Please try again in a moment.",
      });
      res.end();
      return;
    }

    const data = await djangoResp.json();
    const text = (data && data.text) || '';
    // Phase 2 MVP: single chunk with the full reply. Phase 2.5 makes Django
    // stream tokens so the first-byte is << total latency. Connection close
    // signals end-of-stream to avr-core.
    sendChunk({ type: 'text', content: text });
  } catch (err) {
    clearTimeout(timer);
    console.error('[bridge] upstream error:', err && err.message);
    sendChunk({
      type: 'text',
      content: "I'm sorry, I lost the connection. Let me transfer you to our team.",
    });
  } finally {
    res.end();
  }
});

app.listen(PORT, () => {
  console.log(
    `[avr-llm-sabiwifi] listening on :${PORT} → ${DJANGO_URL}/api/ai/voice-turn/`,
  );
});
