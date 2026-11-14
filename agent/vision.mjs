// One vision request to the Gemini API (generateContent, free tier, GOOGLE_API_KEY): image in,
// card fields out as JSON constrained by CARD_SCHEMA. The key is sent only in the x-goog-api-key
// header and is never logged.
import { CARD_SCHEMA, VISION_PROMPT } from './card-reader.mjs';

// Confirmed 2026-11-14 in Google's model docs: stable, image input, structured output, and "minimal"
// thinking for low latency. Listed by GET /v1beta/models for this key. Override with VISION_MODEL.
export const DEFAULT_VISION_MODEL = 'gemini-3.5-flash-lite';
const API = 'https://generativelanguage.googleapis.com/v1beta/models';

export class VisionError extends Error {
  /** @param {string} code @param {string} message @param {number | null} [retryAfterMs] */
  constructor(code, message, retryAfterMs = null) {
    super(message);
    this.code = code;
    this.retryAfterMs = retryAfterMs;
  }
}

/** Reads "retryDelay": "12s" from a Gemini 429 error body, if present. */
function retryDelayMs(body) {
  const d = body?.error?.details?.find(x => typeof x?.retryDelay === 'string')?.retryDelay;
  const m = typeof d === 'string' ? /^([\d.]+)s$/.exec(d) : null;
  return m ? Math.ceil(Number(m[1]) * 1000) : null;
}

/**
 * @param {Buffer} png grayscale PNG bytes (in memory only)
 * @param {{ model?: string, apiKey?: string, timeoutMs?: number, thinkingLevel?: string | null }} [opts]
 * @returns {Promise<string>} the model's raw JSON text (parse it with parseCardReading)
 */
export async function readCardImage(png, opts = {}) {
  const model = opts.model || process.env.VISION_MODEL || DEFAULT_VISION_MODEL;
  const apiKey = opts.apiKey ?? process.env.GOOGLE_API_KEY;
  if (!apiKey) throw new VisionError('no_key', 'GOOGLE_API_KEY is not set');
  const thinkingLevel = opts.thinkingLevel === undefined ? (process.env.VISION_THINKING ?? 'minimal') : opts.thinkingLevel;
  // No temperature override: with temperature 0, gemini-3.5-flash-lite took 20-60 s (or hung) on some
  // card images that answer in about 1.5 s at the default (measured 2026-11-14). A typical answer is
  // about 110 tokens; the cap stops any runaway output.
  const generationConfig = {
    maxOutputTokens: 400,
    responseMimeType: 'application/json',
    responseJsonSchema: CARD_SCHEMA,
    ...(thinkingLevel ? { thinkingConfig: { thinkingLevel } } : {}),
  };
  let res;
  try {
    res = await fetch(`${API}/${encodeURIComponent(model)}:generateContent`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-goog-api-key': apiKey },
      body: JSON.stringify({
        contents: [{
          role: 'user',
          parts: [
            { text: VISION_PROMPT },
            { inlineData: { mimeType: 'image/png', data: png.toString('base64') } },
          ],
        }],
        generationConfig,
      }),
      signal: AbortSignal.timeout(opts.timeoutMs ?? 10_000),
    });
  } catch (e) {
    const name = /** @type {any} */ (e)?.name;
    throw new VisionError(name === 'TimeoutError' ? 'timeout' : 'network', String(/** @type {any} */ (e)?.message ?? e));
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const msg = String(body?.error?.message ?? res.statusText).slice(0, 300);
    if (res.status === 429) throw new VisionError('rate_limited', msg, retryDelayMs(body));
    throw new VisionError(`http_${res.status}`, msg);
  }
  const candidate = body?.candidates?.[0];
  const text = candidate?.content?.parts?.filter(p => typeof p.text === 'string' && !p.thought).map(p => p.text).join('') ?? '';
  if (!text) throw new VisionError('empty', `no text (finishReason=${candidate?.finishReason ?? 'none'}, block=${body?.promptFeedback?.blockReason ?? 'none'})`);
  return text;
}
