/**
 * Patched ElevenLabs TTS for avr-core.
 *
 * Drop-in replacement for /usr/src/app/index.js in the
 * agentvoiceresponse/avr-tts-elevenlabs image.
 *
 * Why: the upstream uses the `elevenlabs` npm SDK v1.59.0 which receives
 * 402 Payment Required from the ElevenLabs API even when direct fetch with
 * the same key/voice/model returns 200. Verified empirically — the SDK's
 * wire format trips a paid-tier gate that direct fetch does not.
 *
 * This rewrite uses node 20+'s built-in fetch and mirrors the original
 * surface: POST /text-to-speech-stream with { text } returns audio bytes.
 */
const express = require('express');

const PORT = parseInt(process.env.PORT || '6007', 10);
const VOICE_ID = process.env.ELEVENLABS_VOICE_ID;
const MODEL_ID = process.env.ELEVENLABS_MODEL_ID || 'eleven_turbo_v2_5';
const API_KEY = process.env.ELEVENLABS_API_KEY;
const OUTPUT_FORMAT = 'pcm_8000';

const app = express();
app.use(express.json());

app.post('/text-to-speech-stream', async (req, res) => {
  const { text } = req.body || {};
  if (!text) return res.status(400).json({ message: 'Text is required' });
  if (!VOICE_ID || !API_KEY) {
    return res
      .status(500)
      .json({ message: 'ELEVENLABS_VOICE_ID and ELEVENLABS_API_KEY required' });
  }

  console.log('TTS request:', { voice: VOICE_ID, model: MODEL_ID, len: text.length });

  res.setHeader('Content-Type', 'audio/basic');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');

  const url =
    `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(VOICE_ID)}/stream` +
    `?output_format=${OUTPUT_FORMAT}`;

  try {
    const upstream = await fetch(url, {
      method: 'POST',
      headers: {
        'xi-api-key': API_KEY,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ text, model_id: MODEL_ID }),
    });

    if (!upstream.ok) {
      const body = await upstream.text();
      console.error(`ElevenLabs ${upstream.status}:`, body.slice(0, 300));
      return res.status(500).json({
        message: 'Error processing text-to-speech request',
        error: `Status code: ${upstream.status}\nBody: ${body.slice(0, 300)}`,
      });
    }

    const reader = upstream.body.getReader();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      res.write(value);
    }
    res.end();
  } catch (err) {
    console.error('TTS fetch error:', err && err.message);
    if (!res.headersSent) {
      res.status(500).json({
        message: 'Error processing text-to-speech request',
        error: err && err.message,
      });
    } else {
      res.end();
    }
  }
});

app.listen(PORT, () => {
  console.log(`[tts-elevenlabs-patch] listening on :${PORT} voice=${VOICE_ID} model=${MODEL_ID}`);
});
