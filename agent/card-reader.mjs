// Staff-card reading logic (TriageX Stage C, handoff Prompt 3). Pure functions and a small state
// machine, with no LiveKit or network code, so it can be unit-tested with recorded model outputs.
//
// The agent only EXTRACTS fields from the card. It never decides whether a card is valid: the backend
// checks the card ID against the synthetic roster (Prompt 4).

/** JSON schema the vision model must answer with (Gemini structured output, JSON Schema subset). */
export const CARD_SCHEMA = {
  type: 'object',
  properties: {
    card_present: { type: 'boolean', description: 'True only if a TriageX-style staff card is clearly visible in the image.' },
    header_says_triagex_demo: { type: 'boolean', description: 'True only if the card header text reads "TriageX DEMO".' },
    card_id: { type: ['string', 'null'], description: 'The staff ID exactly as printed after "ID", e.g. TX-RN-1001. Null if not legible.' },
    name: { type: ['string', 'null'], description: 'The name line exactly as printed. Null if not legible.' },
    role: { type: ['string', 'null'], description: 'The role line exactly as printed. Null if not legible.' },
    availability: { type: ['string', 'null'], description: 'The availability word(s) exactly as printed, e.g. AVAILABLE or OFF DUTY. Null if not legible.' },
    shift: { type: ['string', 'null'], description: 'The shift line exactly as printed. Null if not present or not legible.' },
    confidence: { type: 'number', minimum: 0, maximum: 1, description: 'Confidence (0 to 1) that card_id is read exactly right.' },
  },
  required: ['card_present', 'header_says_triagex_demo', 'card_id', 'name', 'role', 'availability', 'shift', 'confidence'],
  additionalProperties: false,
};

export const VISION_PROMPT = `You read fictional staff cards in one frame of a live phone video call.
The image is a grayscale crop from the centre of the caller's camera. It may contain a hand, a room,
a computer screen showing a card, glare, blur, or video-call controls.

A staff card has a header "TriageX DEMO", the words "STAFF CARD", a line "FICTIONAL · NOT A REAL CREDENTIAL",
an ID such as "TX-RN-1001", a name, a role, an availability word such as AVAILABLE or OFF DUTY, and a shift line.

Rules:
- Transcribe only text you can actually read in THIS image. Never guess or complete characters you cannot see.
- If any character of the ID is unreadable, set card_id to null and lower confidence.
- If no card is visible (blank page, hand, face, room), set card_present to false, all text fields to null and confidence to 0.
- confidence is your probability that card_id is exactly right, character for character.
- The image may be mirrored or rotated; read the text as printed on the card.
- Do not judge whether the card is valid. Only transcribe it.`;

export const MIN_CONFIDENCE = 0.8;
export const REQUIRED_CONSECUTIVE = 2;
export const TIMEOUT_MS = 45_000;
/** Minimum time between two voice nudges, and before the first one (so the greeting is not cut off). */
export const NUDGE_INTERVAL_MS = 9_000;

const TEXT_FIELDS = ['card_id', 'name', 'role', 'availability', 'shift'];

/**
 * Normalizes how a card ID is written, without changing any character the model read: upper case,
 * surrounding space removed, and runs of spaces / dashes / dots between letter and digit groups
 * turned into one ASCII hyphen ("tx rn 1001", "TX–RN–1001" -> "TX-RN-1001"). It never substitutes
 * look-alike characters (O for 0, I for 1): an ID that does not match is passed on as read, and the
 * backend rejects it.
 * @param {string | null} id
 */
export function normalizeCardId(id) {
  if (typeof id !== 'string') return null;
  const s = id.normalize('NFKC').trim().toUpperCase()
    .replace(/^ID[\s:.\-]*/, '')
    .replace(/[\s‐-―−._\-]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return s.length ? s : null;
}

/** True when an ID has the printed card format TX-XX-0000 (format only, not roster membership). */
export const looksLikeCardId = id => typeof id === 'string' && /^TX-[A-Z]{2}-\d{4}$/.test(id);

/**
 * Parses and validates one model response against CARD_SCHEMA. Never throws.
 * @param {string} text raw JSON text from the model
 * @returns {{ ok: true, reading: CardReading } | { ok: false, error: string }}
 */
export function parseCardReading(text) {
  let obj;
  try {
    obj = JSON.parse(typeof text === 'string' ? text.trim().replace(/^```(?:json)?\s*|\s*```$/g, '') : '');
  } catch {
    return { ok: false, error: 'not_json' };
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return { ok: false, error: 'not_object' };
  for (const key of CARD_SCHEMA.required) {
    if (!(key in obj)) return { ok: false, error: `missing_${key}` };
  }
  for (const key of Object.keys(obj)) {
    if (!(key in CARD_SCHEMA.properties)) return { ok: false, error: `unexpected_${key}` };
  }
  if (typeof obj.card_present !== 'boolean') return { ok: false, error: 'bad_card_present' };
  if (typeof obj.header_says_triagex_demo !== 'boolean') return { ok: false, error: 'bad_header_says_triagex_demo' };
  for (const key of TEXT_FIELDS) {
    if (obj[key] !== null && typeof obj[key] !== 'string') return { ok: false, error: `bad_${key}` };
  }
  if (typeof obj.confidence !== 'number' || !Number.isFinite(obj.confidence) || obj.confidence < 0 || obj.confidence > 1) {
    return { ok: false, error: 'bad_confidence' };
  }
  const clean = v => {
    if (typeof v !== 'string') return null;
    const s = v.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
    return s || null;
  };
  /** @type {CardReading} */
  const reading = {
    card_present: obj.card_present,
    header_says_triagex_demo: obj.header_says_triagex_demo,
    card_id: normalizeCardId(obj.card_id),
    name: clean(obj.name),
    role: clean(obj.role),
    availability: clean(obj.availability)?.toUpperCase() ?? null,
    shift: clean(obj.shift),
    confidence: obj.confidence,
  };
  // "No card" must not carry a stray ID into the stability check.
  if (!reading.card_present) {
    for (const key of TEXT_FIELDS) reading[key] = null;
    reading.confidence = 0;
  }
  return { ok: true, reading };
}

/**
 * @typedef {{ card_present: boolean, header_says_triagex_demo: boolean, card_id: string | null,
 *   name: string | null, role: string | null, availability: string | null, shift: string | null,
 *   confidence: number }} CardReading
 * @typedef {{ glare?: boolean, dark?: boolean }} FrameQuality
 * @typedef {{ kind: 'reading', reading: CardReading, quality?: FrameQuality }
 *   | { kind: 'no_frame', muted?: boolean }
 *   | { kind: 'error', error: string }} Sample
 * @typedef {'camera_off' | 'no_video' | 'too_dark' | 'no_card' | 'glare' | 'closer' | 'steady'} Hint
 */

/** What the agent says for each hint. These are instructions to the voice model, not literal speech. */
export const HINT_SPEECH = {
  camera_off: 'Tell the caller in one short sentence that their camera seems to be off or paused, and ask them to turn it on and hold their staff card up.',
  no_video: 'Tell the caller in one short sentence that you are not receiving their video right now, and ask them to keep the camera on the staff card.',
  too_dark: 'Tell the caller in one short sentence that the picture is too dark, maybe the lens is covered, and ask them to show the card in better light.',
  no_card: 'Ask the caller in one short sentence to hold their TriageX DEMO staff card up to the camera, about 20 to 30 centimetres away.',
  glare: 'Tell the caller in one short sentence that there is glare on the card, and ask them to tilt it slightly away from the light.',
  closer: 'Ask the caller in one short sentence to hold the card a little closer to the camera and keep it still.',
  steady: 'Ask the caller in one short sentence to hold the card steady for a moment.',
};

/**
 * Accepts a card reading only when the same card_id is extracted in REQUIRED_CONSECUTIVE consecutive
 * samples, each with card_present and confidence >= MIN_CONFIDENCE. Times out after TIMEOUT_MS.
 * A missing frame, a non-card or a low-confidence sample breaks the streak; a failed model call
 * (rate limit, network) is not a sample and leaves the streak unchanged.
 */
export class CardStabilizer {
  /**
   * @param {{ now?: () => number, minConfidence?: number, required?: number, timeoutMs?: number,
   *   nudgeIntervalMs?: number, ignoreCardId?: string | null }} [opts]
   *   ignoreCardId: a card that was just accepted; seeing it again is neither progress nor a problem.
   */
  constructor(opts = {}) {
    this.now = opts.now ?? Date.now;
    this.minConfidence = opts.minConfidence ?? MIN_CONFIDENCE;
    this.required = opts.required ?? REQUIRED_CONSECUTIVE;
    this.timeoutMs = opts.timeoutMs ?? TIMEOUT_MS;
    this.nudgeIntervalMs = opts.nudgeIntervalMs ?? NUDGE_INTERVAL_MS;
    this.ignoreCardId = opts.ignoreCardId ?? null;
    this.startedAt = this.now();
    this.lastNudgeAt = this.startedAt; // no nudge in the first interval
    /** @type {string | null} */ this.streakId = null;
    this.streak = 0;
    /** @type {CardReading[]} */ this.streakReadings = [];
    /** @type {Hint | null} */ this.lastHint = null;
    this.samples = 0;
    this.done = false;
  }

  timedOut() {
    return this.now() - this.startedAt >= this.timeoutMs;
  }

  /**
   * @param {Sample} sample
   * @returns {{ status: 'accepted', reading: CardReading, readings: CardReading[] }
   *   | { status: 'timed_out' }
   *   | { status: 'pending', hint: Hint | null, nudge: Hint | null }}
   */
  observe(sample) {
    if (this.done) throw new Error('CardStabilizer already finished');
    if (this.timedOut()) {
      this.done = true;
      return { status: 'timed_out' };
    }
    if (sample.kind === 'error') return { status: 'pending', hint: null, nudge: null };
    this.samples++;

    /** @type {Hint | null} */
    let hint = null;
    if (sample.kind === 'no_frame') {
      this.#reset();
      hint = sample.muted ? 'camera_off' : 'no_video';
    } else {
      const r = sample.reading;
      const q = sample.quality ?? {};
      const good = r.card_present && r.card_id !== null && r.confidence >= this.minConfidence;
      if (good && r.card_id === this.ignoreCardId) {
        this.#reset();
      } else if (good) {
        if (r.card_id === this.streakId) {
          this.streak++;
          this.streakReadings.push(r);
        } else {
          hint = this.streakId !== null ? 'steady' : null;
          this.streakId = r.card_id;
          this.streak = 1;
          this.streakReadings = [r];
        }
        if (this.streak >= this.required) {
          this.done = true;
          return { status: 'accepted', reading: mergeReadings(this.streakReadings), readings: [...this.streakReadings] };
        }
      } else {
        this.#reset();
        if (q.dark) hint = 'too_dark';
        else if (q.glare && r.card_present) hint = 'glare';
        else if (!r.card_present) hint = q.glare ? 'glare' : 'no_card';
        else hint = 'closer';
      }
    }

    // Nudge only for a problem seen twice in a row (not a one-frame blip), at most once per interval.
    const repeated = hint !== null && hint === this.lastHint;
    this.lastHint = hint;
    const t = this.now();
    if (repeated && t - this.lastNudgeAt >= this.nudgeIntervalMs) {
      this.lastNudgeAt = t;
      return { status: 'pending', hint, nudge: hint };
    }
    return { status: 'pending', hint, nudge: null };
  }

  #reset() {
    this.streakId = null;
    this.streak = 0;
    this.streakReadings = [];
  }
}

/**
 * Combines the accepted readings (same card_id): for each text field, the value from the
 * highest-confidence reading that has one; confidence is the minimum across readings.
 * @param {CardReading[]} readings
 * @returns {CardReading}
 */
export function mergeReadings(readings) {
  const byConfidence = [...readings].sort((a, b) => b.confidence - a.confidence);
  const pick = key => byConfidence.find(r => r[key] !== null)?.[key] ?? null;
  return {
    card_present: true,
    header_says_triagex_demo: readings.every(r => r.header_says_triagex_demo),
    card_id: readings[0].card_id,
    name: pick('name'),
    role: pick('role'),
    availability: pick('availability'),
    shift: pick('shift'),
    confidence: Math.min(...readings.map(r => r.confidence)),
  };
}

/**
 * Client-side request budget (token bucket). The Gemini API free tier allows 15 generateContent
 * requests per minute per model (429 QuotaFailure "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
 * quotaValue 15, measured 2026-11-14), and exceeding it costs a retry delay of up to a minute. Staying
 * under it is much faster than hitting it.
 */
export class RequestBudget {
  /** @param {{ perMinute?: number, burst?: number, now?: () => number }} [opts] */
  constructor(opts = {}) {
    this.now = opts.now ?? Date.now;
    this.perMinute = opts.perMinute ?? 14;
    this.burst = opts.burst ?? 3;
    this.tokens = this.burst;
    this.at = this.now();
  }

  #refill() {
    const t = this.now();
    this.tokens = Math.min(this.burst, this.tokens + ((t - this.at) * this.perMinute) / 60_000);
    this.at = t;
  }

  /** Takes one request if the budget allows it. */
  take() {
    this.#refill();
    if (this.tokens < 1) return false;
    this.tokens -= 1;
    return true;
  }

  /** After a 429: nothing more until the server's delay has passed. */
  drain() {
    this.#refill();
    this.tokens = Math.min(this.tokens, 0);
  }
}

/** Mean absolute difference of two equal-size thumbnails (0..255). */
export function thumbDiff(a, b) {
  if (!a || !b || a.length !== b.length || a.length === 0) return Infinity;
  let sum = 0;
  for (let i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
  return sum / a.length;
}

/** Reads an ID aloud character by character ("T X, R N, 1 0 0 1"), so digits are not merged. */
export function spokenCardId(id) {
  if (!id) return 'unknown';
  return id.split('-').map(part => part.split('').join(' ')).join(', ');
}
