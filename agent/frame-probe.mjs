// Frame probe (TriageX Stage C): proves that real caller camera frames reach this agent.
//
// It logs numbers only (track list, frame size, timestamps, mean luminance), one line per second per
// video track, prefixed "[probe]". It never stores, uploads or forwards a frame. A frame is described
// only while it is fresh: when no new frame has arrived for STALE_MS (camera muted, covered by the
// FaceTime UI, track removed or replaced) it logs "no_current_frame" instead of reusing an old one.
import { RoomEvent, TrackKind, TrackSource, VideoBufferType, VideoStream } from '@livekit/rtc-node';
import { bridgeReady } from './greet.mjs';

const STALE_MS = 2000;
const SAMPLE_MS = 1000;

const kindName = k => TrackKind[k] ?? String(k);
const sourceName = s => TrackSource[s] ?? String(s);

/** Mean luminance (0..255, Rec. 601) of a frame, sampling every 8th pixel. */
export function meanLuma(frame) {
  const rgba = frame.type === VideoBufferType.RGBA ? frame : frame.convert(VideoBufferType.RGBA);
  const d = rgba.data;
  let sum = 0;
  let n = 0;
  for (let i = 0; i + 2 < d.length; i += 4 * 8) {
    sum += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
    n++;
  }
  return n ? sum / n : 0;
}

/**
 * Starts probing every remote video track in the room. Call before ctx.connect() so no early
 * subscription is missed, then call snapshot('connected') after connecting.
 * @returns {{ snapshot(reason: string): void, stop(): void }}
 */
export function startFrameProbe(room, log = line => console.log(line)) {
  const emit = (event, data = {}) =>
    log(`[probe] ${JSON.stringify({ probe: event, at: new Date().toISOString(), ...data })}`);
  const describe = (pub, participant) => ({
    participant: participant?.identity,
    sid: pub.sid,
    name: pub.name,
    kind: kindName(pub.kind),
    source: sourceName(pub.source),
    muted: pub.muted,
    mime: pub.mimeType,
    width: pub.width,
    height: pub.height,
  });

  /** @type {Map<string, {sid: string, participant: string, name?: string, reader?: ReadableStreamDefaultReader, frames: number, windowFrames: number, windowStart: number, lastFrameAt: number, lastSampleAt: number, muted: boolean, closed: boolean}>} */
  const tracks = new Map();
  let stopped = false;

  async function pump(track, pub, participant) {
    const sid = pub.sid ?? track.sid;
    if (!sid || tracks.has(sid)) return;
    const st = {
      sid,
      participant: participant.identity,
      name: pub.name,
      frames: 0,
      windowFrames: 0,
      windowStart: Date.now(),
      lastFrameAt: 0,
      lastSampleAt: 0,
      muted: Boolean(pub.muted),
      closed: false,
    };
    tracks.set(sid, st);
    emit('video_stream_open', { sid, participant: st.participant, name: st.name });
    const reader = new VideoStream(track).getReader();
    st.reader = reader;
    try {
      while (!stopped) {
        const { value, done } = await reader.read();
        if (done || !value) break;
        const now = Date.now();
        st.frames++;
        st.windowFrames++;
        st.lastFrameAt = now;
        if (now - st.lastSampleAt < SAMPLE_MS) continue;
        const f = value.frame;
        const elapsed = Math.max(1, now - st.windowStart);
        emit('frame', {
          sid,
          participant: st.participant,
          width: f.width,
          height: f.height,
          buffer: VideoBufferType[f.type] ?? f.type,
          rotation: value.rotation,
          timestamp_us: value.timestampUs.toString(),
          luma: Math.round(meanLuma(f) * 10) / 10,
          fps: Math.round((st.windowFrames * 1000 * 10) / elapsed) / 10,
          total_frames: st.frames,
        });
        st.lastSampleAt = now;
        st.windowFrames = 0;
        st.windowStart = now;
      }
    } catch (e) {
      emit('video_stream_error', { sid, error: String(e?.message ?? e) });
    } finally {
      st.closed = true;
      tracks.delete(sid);
      emit('video_stream_closed', { sid, participant: st.participant, total_frames: st.frames });
    }
  }

  function stopTrack(sid) {
    const st = tracks.get(sid);
    if (!st) return;
    st.reader?.cancel().catch(() => {});
  }

  const onPublished = (pub, p) => emit('track_published', describe(pub, p));
  const onUnpublished = (pub, p) => { emit('track_unpublished', describe(pub, p)); stopTrack(pub.sid); };
  const onSubscribed = (track, pub, p) => {
    emit('track_subscribed', describe(pub, p));
    if (pub.kind === TrackKind.KIND_VIDEO) void pump(track, pub, p);
  };
  const onUnsubscribed = (_track, pub, p) => { emit('track_unsubscribed', describe(pub, p)); stopTrack(pub.sid); };
  const onSubscriptionFailed = (sid, p, reason) => emit('track_subscription_failed', { sid, participant: p?.identity, reason });
  const onMuted = (pub, p) => {
    emit('track_muted', describe(pub, p));
    const st = tracks.get(pub.sid);
    if (st) st.muted = true;
  };
  const onUnmuted = (pub, p) => {
    emit('track_unmuted', describe(pub, p));
    const st = tracks.get(pub.sid);
    if (st) st.muted = false;
  };
  const onParticipant = p => emit('participant_connected', { participant: p.identity, kind: p.kind });
  const onParticipantGone = p => emit('participant_disconnected', { participant: p.identity });
  const onMetadata = () => { if (bridgeReady(room.metadata)) snapshot('bridge_ready'); };

  room
    .on(RoomEvent.TrackPublished, onPublished)
    .on(RoomEvent.TrackUnpublished, onUnpublished)
    .on(RoomEvent.TrackSubscribed, onSubscribed)
    .on(RoomEvent.TrackUnsubscribed, onUnsubscribed)
    .on(RoomEvent.TrackSubscriptionFailed, onSubscriptionFailed)
    .on(RoomEvent.TrackMuted, onMuted)
    .on(RoomEvent.TrackUnmuted, onUnmuted)
    .on(RoomEvent.ParticipantConnected, onParticipant)
    .on(RoomEvent.ParticipantDisconnected, onParticipantGone)
    .on(RoomEvent.RoomMetadataChanged, onMetadata);

  // Once per second: report every video track that has no fresh frame. Never describes an old frame.
  const watchdog = setInterval(() => {
    const now = Date.now();
    for (const st of tracks.values()) {
      const age = st.lastFrameAt ? now - st.lastFrameAt : null;
      if (age === null || age > STALE_MS) {
        emit('no_current_frame', { sid: st.sid, participant: st.participant, muted: st.muted, ms_since_last_frame: age });
      }
    }
  }, SAMPLE_MS);

  function snapshot(reason) {
    const participants = [...(room.remoteParticipants?.values() ?? [])].map(p => ({
      participant: p.identity,
      tracks: [...p.trackPublications.values()].map(pub => ({ ...describe(pub, p), subscribed: pub.subscribed })),
    }));
    emit('room_snapshot', { reason, participants });
    // Subscriptions that completed before our handlers saw them.
    for (const p of room.remoteParticipants?.values() ?? []) {
      for (const pub of p.trackPublications.values()) {
        if (pub.kind === TrackKind.KIND_VIDEO && pub.track && !tracks.has(pub.sid)) void pump(pub.track, pub, p);
      }
    }
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    clearInterval(watchdog);
    for (const sid of [...tracks.keys()]) stopTrack(sid);
    room
      .off(RoomEvent.TrackPublished, onPublished)
      .off(RoomEvent.TrackUnpublished, onUnpublished)
      .off(RoomEvent.TrackSubscribed, onSubscribed)
      .off(RoomEvent.TrackUnsubscribed, onUnsubscribed)
      .off(RoomEvent.TrackSubscriptionFailed, onSubscriptionFailed)
      .off(RoomEvent.TrackMuted, onMuted)
      .off(RoomEvent.TrackUnmuted, onUnmuted)
      .off(RoomEvent.ParticipantConnected, onParticipant)
      .off(RoomEvent.ParticipantDisconnected, onParticipantGone)
      .off(RoomEvent.RoomMetadataChanged, onMetadata);
  }

  return { snapshot, stop };
}
