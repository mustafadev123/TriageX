// The live sampling loop with a fake room, a fake frame stream and a fake model (recorded outputs).
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { RoomEvent, TrackKind } from '@livekit/rtc-node';
import { VideoBufferType, VideoFrame } from '@livekit/rtc-node';
import { RequestBudget } from '../card-reader.mjs';
import { prepareFrame, startCardReader } from '../card-session.mjs';

const recorded = Object.fromEntries(
  JSON.parse(readFileSync(new URL('./fixtures/gemini-card-outputs.json', import.meta.url), 'utf8')).outputs.map(o => [o.scene, o.raw]),
);
const flush = () => new Promise(r => setImmediate(r));

/** Controllable stream of frames for one fake track. */
function fakeStream() {
  const queue = [];
  let wake = null;
  let closed = false;
  return {
    closed: () => closed,
    push(frame) { queue.push({ frame, rotation: 0 }); wake?.(); },
    close() { closed = true; wake?.(); },
    async *[Symbol.asyncIterator]() {
      while (!closed) {
        if (queue.length) { yield queue.shift(); continue; }
        await new Promise(r => { wake = r; });
        wake = null;
      }
    },
  };
}

function setup({ readImage, budget, thumbs = {} } = {}) {
  let t = 1_000_000;
  const room = new EventEmitter();
  room.remoteParticipants = new Map();
  const streams = new Map();
  const events = [];
  const lines = [];
  const calls = [];
  const reader = startCardReader(room, {
    onEvent: e => events.push(e),
    log: l => lines.push(JSON.parse(l.slice('[card] '.length))),
    now: () => t,
    sampleMs: 3_600_000, // ticks are driven by the test
    openFrames: track => { const s = fakeStream(); streams.set(track.sid, s); return s; },
    // The fake "frame" is the scene name; prepare passes it through as the PNG.
    prepare: frame => ({ png: frame, quality: { luma: 120, clipped: 0, glare: false, dark: frame === 'dark' }, width: 1024, height: 576, thumb: thumbs[frame] }),
    budget: budget ?? { take: () => true, drain() {} }, // unlimited unless a test passes a RequestBudget
    readImage: readImage ?? (png => { calls.push(png); return Promise.resolve(recorded[png]); }),
  });
  const publish = (sid, name = 'facetime-remote-video') => {
    const pub = { sid, name, kind: TrackKind.KIND_VIDEO, muted: false };
    room.emit(RoomEvent.TrackSubscribed, { sid }, pub, { identity: 'chert-bridge-test' });
    return pub;
  };
  return { room, reader, events, lines, calls, streams, publish, now: () => t, advance: ms => { t += ms; } };
}

const samples = lines => lines.filter(l => l.card === 'sample');

test('no video yet: no_frame samples, then a card read from two live frames', async () => {
  const s = setup();
  s.reader.scan();
  s.reader.tick();
  assert.equal(samples(s.lines).at(-1).kind, 'no_frame');
  assert.equal(samples(s.lines).at(-1).hint, 'no_video');

  s.publish('TR_1');
  s.streams.get('TR_1').push('s1001');
  await flush();
  s.reader.tick(); await flush();
  s.advance(1000);
  s.streams.get('TR_1').push('s1001');
  await flush();
  s.reader.tick(); await flush();
  assert.equal(s.events.length, 1);
  assert.equal(s.events[0].type, 'accepted');
  assert.equal(s.events[0].reading.card_id, 'TX-RN-1001');
  assert.equal(s.reader.state, 'accepted');
  assert.equal(s.calls.length, 2);
  s.reader.stop();
});

test('blank page: card_present=false and never accepted', async () => {
  const s = setup();
  s.publish('TR_1');
  s.reader.scan();
  for (let i = 0; i < 5; i++) {
    s.streams.get('TR_1').push('sblank');
    await flush();
    s.reader.tick(); await flush();
    s.advance(1000);
  }
  assert.equal(s.events.length, 0);
  const last = samples(s.lines).at(-1);
  assert.equal(last.card_present, false);
  assert.equal(last.hint, 'no_card');
  s.reader.stop();
});

test('results are applied in sample order: an overtaken result is dropped', async () => {
  const pending = [];
  const s = setup({ readImage: png => new Promise(resolve => pending.push(() => resolve(recorded[png]))) });
  s.publish('TR_1');
  s.reader.scan();
  for (const scene of ['s1002', 's1001', 's1001']) {
    s.streams.get('TR_1').push(scene);
    await flush();
    s.reader.tick();
    s.advance(1000);
  }
  pending[1](); await flush(); // sample 2 (1001) returns first
  pending[0](); await flush(); // sample 1 (1002) is older: dropped
  assert.equal(s.lines.filter(l => l.card === 'sample_dropped').length, 1);
  assert.equal(s.events.length, 0);
  pending[2](); await flush(); // sample 3 (1001): two consecutive applied samples agree
  assert.equal(s.events[0]?.reading.card_id, 'TX-RN-1001');
  s.reader.stop();
});

test('muted camera: camera_off, and the frame from before the mute is never reused', async () => {
  const s = setup();
  const pub = s.publish('TR_1');
  s.reader.scan();
  s.streams.get('TR_1').push('s1001');
  await flush();
  s.reader.tick(); await flush();
  s.room.emit(RoomEvent.TrackMuted, pub, {});
  s.reader.tick(); await flush();
  assert.equal(samples(s.lines).at(-1).kind, 'no_frame');
  assert.equal(samples(s.lines).at(-1).hint, 'camera_off');
  s.room.emit(RoomEvent.TrackUnmuted, pub, {});
  s.reader.tick(); await flush(); // unmuted, but no new frame yet
  assert.equal(samples(s.lines).at(-1).kind, 'no_frame');
  assert.equal(s.calls.length, 1);
  assert.equal(s.events.length, 0);
  s.reader.stop();
});

test('stale frame (no new frame for over 2 s) is not sent to the model', async () => {
  const s = setup();
  s.publish('TR_1');
  s.reader.scan();
  s.streams.get('TR_1').push('s1001');
  await flush();
  s.advance(2500);
  s.reader.tick(); await flush();
  assert.equal(samples(s.lines).at(-1).kind, 'no_frame');
  assert.equal(s.calls.length, 0);
  s.reader.stop();
});

test('track replaced: the old stream is closed and frames come from the new track', async () => {
  const s = setup();
  s.publish('TR_1');
  s.reader.scan();
  s.streams.get('TR_1').push('s9999');
  await flush();
  s.reader.tick(); await flush();
  s.publish('TR_2');
  assert.ok(s.streams.get('TR_1').closed());
  assert.ok(s.lines.some(l => l.card === 'track_replaced' && l.new_sid === 'TR_2'));
  s.reader.tick(); await flush(); // new track has no frame yet: the old 9999 frame is not reused
  assert.equal(samples(s.lines).at(-1).kind, 'no_frame');
  s.streams.get('TR_2').push('s1002'); await flush();
  s.reader.tick(); await flush();
  s.advance(1000);
  s.streams.get('TR_2').push('s1002'); await flush();
  s.reader.tick(); await flush();
  assert.equal(s.events[0]?.reading.card_id, 'TX-RN-1002');
  s.reader.stop();
});

test('covered lens is handled locally (no model call) as too dark', async () => {
  const s = setup();
  s.publish('TR_1');
  s.reader.scan();
  s.streams.get('TR_1').push('dark'); await flush();
  s.reader.tick(); await flush();
  const last = samples(s.lines).at(-1);
  assert.equal(last.source, 'local_dark');
  assert.equal(last.hint, 'too_dark');
  assert.equal(s.calls.length, 0);
  s.reader.stop();
});

test('45 s timeout fires even when no result arrives', async () => {
  const s = setup({ readImage: () => new Promise(() => {}) });
  s.publish('TR_1');
  s.reader.scan();
  s.advance(45_000);
  s.reader.tick();
  assert.deepEqual(s.events, [{ type: 'timed_out' }]);
  assert.equal(s.reader.state, 'timed_out');
  s.reader.stop();
});

test('rate limit: sampling pauses for the server-given delay', async () => {
  const s = setup({ readImage: () => Promise.reject(Object.assign(new Error('quota'), { code: 'rate_limited', retryAfterMs: 5000 })) });
  s.publish('TR_1');
  s.reader.scan();
  s.streams.get('TR_1').push('s1001'); await flush();
  s.reader.tick(); await flush();
  assert.match(samples(s.lines).at(-1).error, /^rate_limited/);
  const before = samples(s.lines).length;
  s.advance(1000);
  s.streams.get('TR_1').push('s1001'); await flush();
  s.reader.tick(); await flush();
  assert.equal(samples(s.lines).length, before); // paused
  s.reader.stop();
});

test('a new scan ignores results from the previous scan', async () => {
  const pending = [];
  const s = setup({ readImage: png => new Promise(resolve => pending.push(() => resolve(recorded[png]))) });
  s.publish('TR_1');
  s.reader.scan();
  s.streams.get('TR_1').push('s1001'); await flush();
  s.reader.tick();
  s.reader.scan({ ignoreCardId: 'TX-RN-1001' });
  pending[0](); await flush();
  assert.equal(samples(s.lines).length, 0);
  s.reader.stop();
});

test('unchanged empty scene is not re-sent for 8 s; a changed scene is sent at once', async () => {
  const blank = new Uint8Array(32 * 18).fill(200);
  const s = setup({ thumbs: { sblank: blank, s1001: new Uint8Array(32 * 18).fill(90) } });
  s.publish('TR_1');
  s.reader.scan();
  const step = async scene => { s.streams.get('TR_1').push(scene); await flush(); s.reader.tick(); await flush(); s.advance(1000); };
  await step('sblank'); // sent: no card
  for (let i = 0; i < 7; i++) await step('sblank'); // same scene, under 8 s: not sent
  assert.equal(s.calls.length, 1);
  await step('sblank'); // 8 s after the last request: sent again
  assert.equal(s.calls.length, 2);
  await step('s1001'); // scene changed (a card appeared): sent at once
  await step('s1001');
  assert.equal(s.calls.length, 4);
  assert.equal(s.events[0]?.reading.card_id, 'TX-RN-1001');
  assert.ok(samples(s.lines).some(l => l.skipped?.unchanged === 7));
  s.reader.stop();
});

test('request budget: at most 3 at once, then about one every 4.3 s (free tier: 15 per minute)', async () => {
  let t = 0;
  const b = new RequestBudget({ now: () => t });
  assert.deepEqual([b.take(), b.take(), b.take(), b.take()], [true, true, true, false]);
  t += 4000;
  assert.equal(b.take(), false);
  t += 300;
  assert.equal(b.take(), true);
  t += 60_000;
  b.drain(); // after a 429
  assert.equal(b.take(), false);
  // In a session, a request the budget refuses is skipped, not counted as a sample.
  const s = setup({ budget: { take: () => false, drain() {} } });
  s.publish('TR_1');
  s.reader.scan();
  s.streams.get('TR_1').push('s1001'); await flush();
  s.reader.tick(); await flush();
  assert.equal(s.calls.length, 0);
  assert.equal(samples(s.lines).length, 0);
  s.reader.stop();
});

test('prepareFrame: a real 1280x720 I420 frame becomes a 1024x576 PNG and a 32x18 thumbnail, in memory', () => {
  const w = 1280, h = 720;
  const data = new Uint8Array(w * h * 1.5).fill(128); // I420: Y plane, then U and V
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) data[y * w + x] = x < w / 2 ? 40 : 220;
  const p = prepareFrame(new VideoFrame(data, w, h, VideoBufferType.I420), 0);
  assert.equal(p.width, 1024);
  assert.equal(p.height, 576);
  assert.equal(p.png.toString('ascii', 1, 4), 'PNG');
  assert.equal(p.thumb.length, 32 * 18);
  assert.equal(p.thumb[0], 40);
  assert.equal(p.thumb[31], 220);
});
