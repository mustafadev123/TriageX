// Live staff-card reading from the caller's FaceTime video (TriageX Stage C, handoff Prompt 3).
//
// While a scan is active ("awaiting card"), at most one frame per second is taken from the caller's
// facetime-remote-video track, centre-cropped (FaceTime controls and self-preview sit at the edges),
// downscaled to grayscale and sent to the vision model. Frames live in memory only: nothing is
// written to disk, and only extracted text fields are logged ("[card]" lines).
//
// Free-tier Gemini latency varies from about 1 s to over 30 s, so several requests may run at once.
// Results are applied in sample order: a result that arrives after a newer sample's result was
// already applied is dropped, so an old frame can never complete a reading.
// The free tier allows 15 requests per minute per model, so requests are spent from a client-side
// budget (RequestBudget), and an unchanged scene that just gave "no card" is not sent again for a few
// seconds: the budget is then full when a card appears, and two quick samples can confirm it.
import { RoomEvent, TrackKind, VideoBufferType, VideoRotation, VideoStream } from '@livekit/rtc-node';
import { CardStabilizer, parseCardReading, RequestBudget, thumbDiff } from './card-reader.mjs';
import { cropAndScale, encodeGrayPng, lumaPlane, quality } from './frame-utils.mjs';
import { readCardImage } from './vision.mjs';

export const CALLER_VIDEO_TRACK = 'facetime-remote-video';
const STALE_MS = 2000;
/** @type {import('./card-reader.mjs').CardReading} */
const NO_CARD = Object.freeze({ card_present: false, header_says_triagex_demo: false, card_id: null, name: null, role: null, availability: null, shift: null, confidence: 0 });
const ROTATION_DEG = { [VideoRotation.VIDEO_ROTATION_0]: 0, [VideoRotation.VIDEO_ROTATION_90]: 90, [VideoRotation.VIDEO_ROTATION_180]: 180, [VideoRotation.VIDEO_ROTATION_270]: 270 };

/** Default frame source: a LiveKit VideoStream as an async iterable of { frame, rotation }. */
function openVideoStream(track) {
  const reader = new VideoStream(track).getReader();
  return {
    async *[Symbol.asyncIterator]() {
      while (true) {
        const { value, done } = await reader.read();
        if (done || !value) return;
        yield value;
      }
    },
    close: () => reader.cancel().catch(() => {}),
  };
}

/** Turns one LiveKit frame into the PNG and quality numbers sent with a sample. In memory only. */
export function prepareFrame(frame, rotation = 0) {
  const { y, width, height } = lumaPlane(frame, VideoBufferType, VideoBufferType.I420);
  const img = cropAndScale(y, width, height, { rotation: ROTATION_DEG[rotation] ?? 0 });
  const thumb = cropAndScale(img.data, img.width, img.height, { cropFraction: 1, maxSide: 32 }).data;
  return { png: encodeGrayPng(img.data, img.width, img.height), quality: quality(img.data), width: img.width, height: img.height, thumb };
}

/**
 * @typedef {{ type: 'nudge', hint: import('./card-reader.mjs').Hint }
 *   | { type: 'accepted', reading: import('./card-reader.mjs').CardReading }
 *   | { type: 'timed_out' }} CardEvent
 */

/**
 * Starts watching the room's caller video. Call before ctx.connect() so no subscription is missed.
 * @param {any} room LiveKit Room
 * @param {{
 *   onEvent: (e: CardEvent) => void,
 *   log?: (line: string) => void,
 *   readImage?: (png: Buffer, opts: { timeoutMs: number }) => Promise<string>,
 *   openFrames?: (track: any) => AsyncIterable<{ frame: any, rotation?: number }> & { close(): void },
 *   prepare?: typeof prepareFrame,
 *   now?: () => number,
 *   sampleMs?: number, maxInFlight?: number, requestTimeoutMs?: number,
 *   budget?: RequestBudget, changeThreshold?: number, idleResendMs?: number,
 *   stabilizer?: ConstructorParameters<typeof CardStabilizer>[0],
 * }} opts
 */
export function startCardReader(room, opts) {
  const log = opts.log ?? (line => console.log(line));
  const now = opts.now ?? Date.now;
  const readImage = opts.readImage ?? ((png, o) => readCardImage(png, o));
  const openFrames = opts.openFrames ?? openVideoStream;
  const prepare = opts.prepare ?? prepareFrame;
  const sampleMs = Math.max(1000, opts.sampleMs ?? 1000); // never more than one frame per second
  const maxInFlight = opts.maxInFlight ?? 4;
  const requestTimeoutMs = opts.requestTimeoutMs ?? 15_000;
  const budget = opts.budget ?? new RequestBudget({ now, perMinute: Number(process.env.VISION_RPM) || 14 });
  const changeThreshold = opts.changeThreshold ?? 6; // mean abs. luma difference of 32 px thumbnails
  const idleResendMs = opts.idleResendMs ?? 8000;
  const emit = (event, data = {}) => log(`[card] ${JSON.stringify({ card: event, at: new Date(now()).toISOString(), ...data })}`);

  /** The caller video track being read, and only its latest frame. */
  /** @type {{ sid: string, stream: any, muted: boolean, latest: { frame: any, rotation: number, at: number } | null } | null} */
  let video = null;
  let stopped = false;

  /** @type {'idle' | 'awaiting' | 'accepted' | 'timed_out'} */
  let state = 'idle';
  let scanId = 0;
  /** @type {CardStabilizer | null} */ let stabilizer = null;
  let seq = 0, appliedSeq = 0, inFlight = 0, backoffUntil = 0;
  /** Thumbnail of the last frame that the model said shows no card, and when it was sent. */
  /** @type {Uint8Array | null} */ let noCardThumb = null;
  let noCardSentAt = 0;
  let skipped = { unchanged: 0, budget: 0, busy: 0 };
  /** @type {ReturnType<typeof setInterval> | null} */ let timer = null;

  // --- Caller video -----------------------------------------------------------------------------
  function isCallerVideo(pub) {
    return pub?.kind === TrackKind.KIND_VIDEO;
  }

  function attach(track, pub, participant) {
    if (!isCallerVideo(pub) || stopped) return;
    const sid = pub.sid ?? track.sid;
    if (video?.sid === sid) return;
    // Prefer the bridge's named track; any other video track is used only if there is nothing else.
    if (video && pub.name !== CALLER_VIDEO_TRACK) return;
    if (video) {
      emit('track_replaced', { old_sid: video.sid, new_sid: sid });
      video.stream.close();
    }
    const stream = openFrames(track);
    const v = { sid, stream, muted: Boolean(pub.muted), latest: null };
    video = v;
    emit('video_attached', { sid, name: pub.name, participant: participant?.identity, muted: v.muted });
    void (async () => {
      try {
        for await (const ev of stream) {
          if (video !== v) break;
          v.latest = { frame: ev.frame, rotation: ev.rotation ?? 0, at: now() };
        }
      } catch (e) {
        emit('video_error', { sid, error: String(e?.message ?? e) });
      } finally {
        if (video === v) {
          video = null;
          emit('video_detached', { sid });
        }
      }
    })();
  }

  function detach(pub) {
    if (!video || video.sid !== pub?.sid) return;
    const v = video;
    video = null;
    v.stream.close();
    emit('video_detached', { sid: v.sid });
  }

  const onSubscribed = (track, pub, participant) => attach(track, pub, participant);
  const onUnsubscribed = (_track, pub) => detach(pub);
  const onUnpublished = pub => detach(pub);
  const onMuted = pub => {
    if (video && video.sid === pub?.sid) { video.muted = true; video.latest = null; emit('video_muted', { sid: pub.sid }); }
  };
  const onUnmuted = pub => {
    if (video && video.sid === pub?.sid) { video.muted = false; emit('video_unmuted', { sid: pub.sid }); }
  };
  room
    .on(RoomEvent.TrackSubscribed, onSubscribed)
    .on(RoomEvent.TrackUnsubscribed, onUnsubscribed)
    .on(RoomEvent.TrackUnpublished, onUnpublished)
    .on(RoomEvent.TrackMuted, onMuted)
    .on(RoomEvent.TrackUnmuted, onUnmuted);

  /** Picks up video tracks that were subscribed before the handlers saw them. */
  function syncTracks() {
    for (const p of room.remoteParticipants?.values() ?? []) {
      for (const pub of p.trackPublications.values()) {
        if (isCallerVideo(pub) && pub.track) attach(pub.track, pub, p);
      }
    }
  }

  /** The latest frame, only if it is fresh and the track is not muted. Never an old frame. */
  function currentFrame() {
    if (!video) return { frame: null, muted: false };
    if (video.muted) return { frame: null, muted: true };
    const l = video.latest;
    if (!l || now() - l.at > STALE_MS) return { frame: null, muted: false };
    return { frame: l, muted: false };
  }

  // --- Scanning ---------------------------------------------------------------------------------
  function finish(nextState) {
    state = nextState;
    stabilizer = null;
    if (timer) clearInterval(timer);
    timer = null;
  }

  /** Applies one sample result, in sample order. Older results that arrive late are dropped. */
  function apply(id, n, sample, extra, sent = null) {
    if (id !== scanId || state !== 'awaiting' || !stabilizer) return;
    if (n <= appliedSeq) {
      const r = sample.kind === 'reading' ? sample.reading : null;
      emit('sample_dropped', { seq: n, reason: 'overtaken', kind: sample.kind, card_present: r?.card_present, card_id: r?.card_id, confidence: r?.confidence, ...extra });
      return;
    }
    appliedSeq = n;
    if (sample.kind === 'reading') {
      noCardThumb = !sample.reading.card_present && sent ? sent.thumb : null;
      noCardSentAt = sent?.at ?? 0;
    } else if (sample.kind === 'no_frame') {
      noCardThumb = null;
    }
    if (skipped.unchanged || skipped.budget || skipped.busy) {
      extra = { ...extra, skipped };
      skipped = { unchanged: 0, budget: 0, busy: 0 };
    }
    const out = stabilizer.observe(sample);
    const fields = sample.kind === 'reading' ? sample.reading : {};
    emit('sample', { seq: n, kind: sample.kind, ...fields, ...(sample.kind === 'error' ? { error: sample.error } : {}), ...(sample.kind === 'no_frame' ? { muted: sample.muted } : {}), quality: sample.quality, ...extra, status: out.status, hint: out.status === 'pending' ? out.hint : undefined, nudge: out.status === 'pending' ? out.nudge ?? undefined : undefined });
    if (out.status === 'accepted') {
      finish('accepted');
      emit('accepted', { ...out.reading, samples: out.readings.length });
      opts.onEvent({ type: 'accepted', reading: out.reading });
    } else if (out.status === 'timed_out') {
      finish('timed_out');
      emit('timed_out', {});
      opts.onEvent({ type: 'timed_out' });
    } else if (out.nudge) {
      opts.onEvent({ type: 'nudge', hint: out.nudge });
    }
  }

  function tick() {
    if (state !== 'awaiting' || !stabilizer) return;
    if (stabilizer.timedOut()) {
      finish('timed_out');
      emit('timed_out', {});
      opts.onEvent({ type: 'timed_out' });
      return;
    }
    if (now() < backoffUntil) return;
    if (inFlight >= maxInFlight) { skipped.busy++; return; }
    const id = scanId;
    syncTracks();
    const { frame, muted } = currentFrame();
    if (!frame) {
      apply(id, ++seq, { kind: 'no_frame', muted }, { video: video ? 'stale' : 'none' });
      return;
    }
    let prepared;
    try {
      prepared = prepare(frame.frame, frame.rotation);
    } catch (e) {
      apply(id, ++seq, { kind: 'error', error: `prepare: ${e?.message ?? e}` }, {});
      return;
    }
    const q = { glare: prepared.quality.glare, dark: prepared.quality.dark };
    const extra = { luma: prepared.quality.luma, clipped: prepared.quality.clipped, img: `${prepared.width}x${prepared.height}` };
    if (prepared.quality.dark) {
      const n = ++seq;
      // Lens covered or no light: nothing can be read, so no model call is spent on it. This is a
      // local exposure check (logged as source "local_dark"), not a recognition result.
      apply(id, n, { kind: 'reading', reading: NO_CARD, quality: q }, { ...extra, source: 'local_dark' });
      return;
    }
    // The same empty scene that just gave "no card": not worth a request yet.
    const change = noCardThumb ? thumbDiff(prepared.thumb, noCardThumb) : Infinity;
    if (change < changeThreshold && now() - noCardSentAt < idleResendMs) { skipped.unchanged++; return; }
    if (!budget.take()) { skipped.budget++; return; }
    const n = ++seq;
    const sent = { thumb: prepared.thumb ?? null, at: now() };
    if (Number.isFinite(change)) extra.change = Math.round(change * 10) / 10;
    inFlight++;
    const t0 = now();
    readImage(prepared.png, { timeoutMs: requestTimeoutMs })
      .then(text => {
        const parsed = parseCardReading(text);
        if (!parsed.ok) return apply(id, n, { kind: 'error', error: `parse: ${parsed.error}` }, { ...extra, ms: now() - t0 });
        apply(id, n, { kind: 'reading', reading: parsed.reading, quality: q }, { ...extra, ms: now() - t0 }, sent);
      })
      .catch(e => {
        if (e?.code === 'rate_limited') { backoffUntil = now() + (e.retryAfterMs ?? 5000); budget.drain(); }
        apply(id, n, { kind: 'error', error: `${e?.code ?? 'error'}: ${String(e?.message ?? e).slice(0, 160)}` }, { ...extra, ms: now() - t0 });
      })
      .finally(() => { inFlight--; });
  }

  /**
   * Starts (or restarts) awaiting a card. Results of an earlier scan are discarded.
   * @param {{ ignoreCardId?: string | null }} [o] ignoreCardId: a card just accepted, still in view
   */
  function scan(o = {}) {
    if (stopped) return;
    scanId++;
    appliedSeq = seq;
    noCardThumb = null;
    skipped = { unchanged: 0, budget: 0, busy: 0 };
    state = 'awaiting';
    stabilizer = new CardStabilizer({ now, ...opts.stabilizer, ignoreCardId: o.ignoreCardId ?? null });
    if (timer) clearInterval(timer);
    timer = setInterval(tick, sampleMs);
    emit('scan_started', { scan: scanId, ignore_card_id: o.ignoreCardId ?? null, model: process.env.VISION_MODEL || undefined });
    syncTracks();
  }

  function cancel() {
    if (state === 'awaiting') emit('scan_cancelled', { scan: scanId });
    scanId++;
    finish('idle');
  }

  function stop() {
    if (stopped) return;
    cancel();
    stopped = true;
    if (video) { video.stream.close(); video = null; }
    room
      .off(RoomEvent.TrackSubscribed, onSubscribed)
      .off(RoomEvent.TrackUnsubscribed, onUnsubscribed)
      .off(RoomEvent.TrackUnpublished, onUnpublished)
      .off(RoomEvent.TrackMuted, onMuted)
      .off(RoomEvent.TrackUnmuted, onUnmuted);
  }

  return {
    scan,
    cancel,
    stop,
    syncTracks,
    /** For tests: run one sampling step now. */
    tick,
    get state() { return state; },
  };
}
