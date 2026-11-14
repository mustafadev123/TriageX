// Parsing and stability logic, driven by recorded model outputs (JSON text, not images).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  CARD_SCHEMA, CardStabilizer, looksLikeCardId, mergeReadings, normalizeCardId, parseCardReading, spokenCardId, thumbDiff,
} from '../card-reader.mjs';

const recorded = JSON.parse(readFileSync(new URL('./fixtures/gemini-card-outputs.json', import.meta.url), 'utf8')).outputs;
const raw = scene => {
  const r = recorded.find(o => o.scene === scene);
  assert.ok(r, `fixture ${scene}`);
  return r.raw;
};
const reading = scene => {
  const p = parseCardReading(raw(scene));
  assert.ok(p.ok, `${scene}: ${p.ok ? '' : p.error}`);
  return p.reading;
};

/** A fake clock for the stabilizer. */
function clock(start = 1_000_000) {
  let t = start;
  return { now: () => t, advance: ms => { t += ms; } };
}
const R = (card_id, confidence = 0.95, extra = {}) => ({
  kind: /** @type {const} */ ('reading'),
  reading: { card_present: true, header_says_triagex_demo: true, card_id, name: 'X (fictional RN)', role: 'Registered Nurse · Emergency', availability: 'AVAILABLE', shift: null, confidence, ...extra },
});
const NONE = { kind: /** @type {const} */ ('reading'), reading: { card_present: false, header_says_triagex_demo: false, card_id: null, name: null, role: null, availability: null, shift: null, confidence: 0 } };

// --- Parsing recorded outputs ---------------------------------------------------------------------

test('recorded outputs: every card ID is extracted exactly', () => {
  assert.equal(reading('s1001').card_id, 'TX-RN-1001');
  assert.equal(reading('s1002').card_id, 'TX-RN-1002');
  assert.equal(reading('s1003m').card_id, 'TX-RN-1003'); // mirrored image
  assert.equal(reading('s9999').card_id, 'TX-RN-9999'); // read as 9999; rejection is the backend's job
  assert.equal(reading('s1004').card_id, 'TX-RN-1004');
  assert.equal(reading('s1004').availability, 'OFF DUTY');
  assert.equal(reading('s1001').name, 'Avery (fictional RN)');
  assert.ok(reading('s1001').header_says_triagex_demo);
});

test('recorded outputs: a blank page is card_present=false with no fields', () => {
  const r = reading('sblank');
  assert.equal(r.card_present, false);
  assert.equal(r.card_id, null);
  assert.equal(r.confidence, 0);
});

test('recorded outputs: glare over the ID gives no card_id and zero confidence', () => {
  const r = reading('s1001g');
  assert.equal(r.card_present, true);
  assert.equal(r.card_id, null);
  assert.equal(r.confidence, 0);
});

test('parse: rejects malformed or out-of-schema outputs', () => {
  const ok = JSON.parse(raw('s1001'));
  const bad = obj => parseCardReading(JSON.stringify(obj));
  assert.deepEqual(parseCardReading('not json'), { ok: false, error: 'not_json' });
  assert.deepEqual(parseCardReading('[1,2]'), { ok: false, error: 'not_object' });
  assert.deepEqual(parseCardReading(''), { ok: false, error: 'not_json' });
  assert.deepEqual(bad({ ...ok, confidence: 1.2 }), { ok: false, error: 'bad_confidence' });
  assert.deepEqual(bad({ ...ok, confidence: '0.9' }), { ok: false, error: 'bad_confidence' });
  assert.deepEqual(bad({ ...ok, card_present: 'yes' }), { ok: false, error: 'bad_card_present' });
  assert.deepEqual(bad({ ...ok, card_id: 1001 }), { ok: false, error: 'bad_card_id' });
  assert.deepEqual(bad({ ...ok, extra: 1 }), { ok: false, error: 'unexpected_extra' });
  const { shift, ...missing } = ok;
  assert.deepEqual(bad(missing), { ok: false, error: 'missing_shift' });
  for (const key of CARD_SCHEMA.required) assert.ok(key in ok, key);
});

test('parse: tolerates a fenced JSON block and trims text', () => {
  const p = parseCardReading('```json\n' + raw('s1002') + '\n```');
  assert.ok(p.ok);
  assert.equal(p.ok && p.reading.card_id, 'TX-RN-1002');
  const q = parseCardReading(JSON.stringify({ ...JSON.parse(raw('s1002')), name: '  Jordan   (fictional RN) ', availability: 'available' }));
  assert.ok(q.ok);
  assert.equal(q.ok && q.reading.name, 'Jordan (fictional RN)');
  assert.equal(q.ok && q.reading.availability, 'AVAILABLE');
});

test('parse: control characters in a field become spaces (seen live: "Registered Nurse \\u0010 Emergency")', () => {
  const p = parseCardReading(JSON.stringify({ ...JSON.parse(raw('s1004')), role: 'Registered Nurse \u0010 Emergency' }));
  assert.ok(p.ok);
  assert.equal(p.ok && p.reading.role, 'Registered Nurse Emergency');
});

test('thumbDiff: mean absolute difference; mismatched sizes count as changed', () => {
  assert.equal(thumbDiff(Uint8Array.from([10, 20]), Uint8Array.from([14, 12])), 6);
  assert.equal(thumbDiff(Uint8Array.from([1]), Uint8Array.from([1, 2])), Infinity);
  assert.equal(thumbDiff(null, Uint8Array.from([1])), Infinity);
});

test('parse: card_present=false clears any stray ID', () => {
  const p = parseCardReading(JSON.stringify({ ...JSON.parse(raw('sblank')), card_id: 'TX-RN-1001', confidence: 0.9 }));
  assert.ok(p.ok);
  assert.equal(p.ok && p.reading.card_id, null);
  assert.equal(p.ok && p.reading.confidence, 0);
});

test('normalizeCardId: spacing and dash variants only, never look-alike characters', () => {
  assert.equal(normalizeCardId('TX-RN-1001'), 'TX-RN-1001');
  assert.equal(normalizeCardId(' tx rn 1001 '), 'TX-RN-1001');
  assert.equal(normalizeCardId('TX–RN–1001'), 'TX-RN-1001');
  assert.equal(normalizeCardId('ID: TX-RN-1001'), 'TX-RN-1001');
  assert.equal(normalizeCardId('TX-RN-1OO1'), 'TX-RN-1OO1'); // passed on as read; the backend rejects it
  assert.equal(normalizeCardId('  '), null);
  assert.equal(normalizeCardId(null), null);
  assert.ok(looksLikeCardId('TX-RN-9999'));
  assert.ok(!looksLikeCardId('TX-RN-1OO1'));
  assert.equal(spokenCardId('TX-RN-1001'), 'T X, R N, 1 0 0 1');
});

// --- Stability ------------------------------------------------------------------------------------

test('accepts the same card_id in 2 consecutive confident samples (recorded outputs)', () => {
  const c = clock();
  const s = new CardStabilizer({ now: c.now });
  const sample = { kind: /** @type {const} */ ('reading'), reading: reading('s1001') };
  assert.equal(s.observe(sample).status, 'pending');
  c.advance(1000);
  const out = s.observe(sample);
  assert.equal(out.status, 'accepted');
  assert.equal(out.status === 'accepted' && out.reading.card_id, 'TX-RN-1001');
  assert.throws(() => s.observe(sample), /already finished/);
});

test('9999 is accepted as read: the agent extracts, the backend validates', () => {
  const s = new CardStabilizer({ now: clock().now });
  const sample = { kind: /** @type {const} */ ('reading'), reading: reading('s9999') };
  s.observe(sample);
  const out = s.observe(sample);
  assert.equal(out.status === 'accepted' && out.reading.card_id, 'TX-RN-9999');
});

test('confidence below 0.8 never counts', () => {
  const s = new CardStabilizer({ now: clock().now });
  for (let i = 0; i < 5; i++) assert.equal(s.observe(R('TX-RN-1001', 0.79)).status, 'pending');
  assert.equal(s.observe(R('TX-RN-1001', 0.8)).status, 'pending');
  assert.equal(s.observe(R('TX-RN-1001', 0.8)).status, 'accepted');
});

test('different IDs, a blank frame or a missing frame break the streak', () => {
  const s = new CardStabilizer({ now: clock().now });
  s.observe(R('TX-RN-1001'));
  assert.equal(s.observe(R('TX-RN-1007')).status, 'pending'); // misread in between
  assert.equal(s.observe(R('TX-RN-1001')).status, 'pending');
  assert.equal(s.observe(NONE).status, 'pending');
  assert.equal(s.observe(R('TX-RN-1001')).status, 'pending');
  assert.equal(s.observe({ kind: 'no_frame', muted: true }).status, 'pending');
  assert.equal(s.observe(R('TX-RN-1001')).status, 'pending');
  assert.equal(s.observe(R('TX-RN-1001')).status, 'accepted');
});

test('a failed model call is not a sample and leaves the streak alone', () => {
  const s = new CardStabilizer({ now: clock().now });
  s.observe(R('TX-RN-1002'));
  assert.deepEqual(s.observe({ kind: 'error', error: 'timeout' }), { status: 'pending', hint: null, nudge: null });
  assert.equal(s.observe(R('TX-RN-1002')).status, 'accepted');
});

test('times out after 45 s', () => {
  const c = clock();
  const s = new CardStabilizer({ now: c.now });
  s.observe(NONE);
  c.advance(44_999);
  assert.equal(s.timedOut(), false);
  assert.equal(s.observe(R('TX-RN-1001')).status, 'pending');
  c.advance(1);
  assert.equal(s.timedOut(), true);
  assert.equal(s.observe(R('TX-RN-1001')).status, 'timed_out');
});

test('merged reading keeps the best fields and the lowest confidence', () => {
  const a = R('TX-RN-1003', 0.95, { name: null, shift: 'Large shift 16:30–20:30 (simulated)' }).reading;
  const b = R('TX-RN-1003', 0.99, { name: 'Morgan (fictional RN)', shift: 'Surge shift 16:30–20:30 (simulated)' }).reading;
  const m = mergeReadings([a, b]);
  assert.equal(m.card_id, 'TX-RN-1003');
  assert.equal(m.name, 'Morgan (fictional RN)');
  assert.equal(m.shift, 'Surge shift 16:30–20:30 (simulated)');
  assert.equal(m.confidence, 0.95);
});

test('ignoreCardId: the card just accepted, still in view, is neither progress nor a problem', () => {
  const s = new CardStabilizer({ now: clock().now, ignoreCardId: 'TX-RN-1001' });
  for (let i = 0; i < 3; i++) assert.deepEqual(s.observe(R('TX-RN-1001')), { status: 'pending', hint: null, nudge: null });
  s.observe(R('TX-RN-1002'));
  assert.equal(s.observe(R('TX-RN-1002')).status, 'accepted');
});

// --- Hints and voice nudges ----------------------------------------------------------------------

test('hints: camera off, no video, dark, no card, glare, closer, steady', () => {
  const s = new CardStabilizer({ now: clock().now });
  const hint = sample => { const o = s.observe(sample); return o.status === 'pending' ? o.hint : o.status; };
  assert.equal(hint({ kind: 'no_frame', muted: true }), 'camera_off');
  assert.equal(hint({ kind: 'no_frame', muted: false }), 'no_video');
  assert.equal(hint({ ...NONE, quality: { dark: true } }), 'too_dark');
  assert.equal(hint(NONE), 'no_card');
  assert.equal(hint({ kind: 'reading', reading: reading('s1001g'), quality: { glare: true } }), 'glare');
  assert.equal(hint({ kind: 'reading', reading: reading('s1001g') }), 'closer');
  assert.equal(hint(R('TX-RN-1001', 0.5)), 'closer');
  assert.equal(hint(R('TX-RN-1001')), null); // first good sample: just wait for the second
  assert.equal(hint(R('TX-RN-1002')), 'steady'); // a different ID: hold still
});

test('nudges: only for a repeated problem, never in the first 9 s, at most once per 9 s', () => {
  const c = clock();
  const s = new CardStabilizer({ now: c.now });
  const nudge = sample => { c.advance(1000); const o = s.observe(sample); return o.status === 'pending' ? o.nudge : o.status; };
  // 0-8 s: blank frames, but the greeting is still playing.
  for (let i = 0; i < 8; i++) assert.equal(nudge(NONE), null);
  assert.equal(nudge(NONE), 'no_card'); // t = 9 s
  for (let i = 0; i < 8; i++) assert.equal(nudge(NONE), null);
  assert.equal(nudge({ kind: 'no_frame', muted: true }), null); // new problem, seen once
  assert.equal(nudge({ kind: 'no_frame', muted: true }), 'camera_off'); // seen twice, 10 s after the last nudge
  assert.equal(nudge(R('TX-RN-1001', 0.3)), null);
  assert.equal(nudge(R('TX-RN-1001', 0.3)), null); // repeated, but within 9 s of the last nudge
});
