# Chert FaceTime status

Last updated: 2026-11-14 (macOS, Node 24.22.3, `@trychert/facetime@0.1.0-beta.21`).
No secrets and no phone numbers are recorded here. Times are US Eastern (UTC−5).

> The **hosted Chert demo agent** was not used for any result below. Every call in this file was
> answered by **our own agent** (`agent/`, `triagex-facetime-agent`) through `chert dev`.

## Status

| Item | Status | Evidence |
|---|---|---|
| Chert authentication | **VERIFIED** | `chert whoami`: signed in, role **owner**, project `36c65de7-…-4398`, `confirmed: true` |
| Line provisioned | **VERIFIED** | `chert doctor`: line "Silas Thorne" available, routing configured, calls enabled, worker Mac checked in |
| iPhone verified (iMessage code) | **VERIFIED** | Completed by the developer in Chert's onboarding; the line accepted two FaceTime Video calls from that iPhone (below) |
| Hosted demo call (`demo_outcome`) | **SKIPPED** | Not run: it proves nothing about our agent and costs trial minutes. The local onboarding record is an expired pre-sign-in session; setup was already complete when checked |
| Trial | **ACTIVE** | `chert doctor`: 47 of 50 FaceTime minutes left after two test calls; "trial line stays yours until your trial ends" |
| LiveKit Cloud (own project) | **VERIFIED** | `chert doctor`: "LiveKit accepted the credentials"; `doctor --agent`: agent joined a test room in 1.2 s. Chert-managed LiveKit: `livekit_not_provisioned` |
| Voice model | **VERIFIED** | Gemini Live `gemini-2.5-flash-native-audio-preview-12-2025`, Gemini API free tier, via `@livekit/agents-plugin-google` 1.8.0 (replaces the starter's paid OpenAI realtime model) |
| Two-way voice with our agent | **VERIFIED** (logs) | Call 1, below. Subjective audio quality not yet reported by the caller |
| Caller camera capture (`facetime-remote-video`) | **VERIFIED** | Call 2, below. Enabled on this line without a request to Chert |
| Our agent receives real caller frames | **VERIFIED** | Call 2: 609 frames, luminance follows the camera |
| Outbound FaceTime (agent places the call) | **UNKNOWN** | Not tested; needs a project API key and `POST /api/v1/calls` (handoff Section 6.4, Phase D2) |
| Project API key / `CHERT_LINE_ID` in `.env` | **PENDING** | Not needed for inbound calls via `chert dev`; needed for outbound calls and the shared-line iMessage adapter (Prompt 5) |

## Call 1: two-way voice (11:08:59–11:10:21, about 80 s)

`chert dev` records: `call_incoming` (FaceTime Video) → `call_accepted` in 0.1 s (warm) →
`call_bridge_ready` after 6.6 s → `call_media`: "Video and audio are coming from" our agent
participant (`video: true`) → `call_ended`.

Agent log: greeting started in the same second as `bridge_ready` (Chert requires < 10 s); Gemini's
server-side voice activity detection registered 5 caller turns and a spoken reply was generated for
each; no errors; the session closed on caller hang-up (`CLIENT_INITIATED`).

## Call 2: caller frames (11:21:16–11:22:11, about 55 s)

Frame probe (`agent/frame-probe.mjs`) excerpt. Numbers only; no image was stored.

```text
11:21:18 participant_connected  chert-bridge-0d536eaa-…
11:21:23 track_published         facetime-remote-video  KIND_VIDEO SOURCE_CAMERA video/VP8 1280x720
11:21:23 track_published         facetime-remote-audio  KIND_AUDIO SOURCE_MICROPHONE audio/opus
11:21:25 room_snapshot (bridge_ready): both tracks present, unmuted
11:21:26 frame 1280x720 I420 ts=109306574000us luma=58.2 fps=14.9 total=49     (normal view)
11:21:32 frame 1280x720 I420 ts=109312692000us luma=75.1 fps=14.5 total=139    (bright)
11:21:45 frame 1280x720 I420 ts=109324865000us luma=14.2 fps=14.8 total=319    (lens covered)
11:21:47 frame 1280x720 I420 ts=109326883000us luma=12.2 fps=14.9 total=349    (lens covered)
11:21:53 frame 1280x720 I420 ts=109332980000us luma=49.9 fps=14.9 total=439    (printed page)
11:22:04 track_muted facetime-remote-video                                       (hang-up)
11:22:06 no_current_frame muted=true ms_since_last_frame=2123                    (no stale frame reused)
11:22:06 video_stream_closed total_frames=609; tracks unpublished; participant_disconnected
```

Findings:
- The bridge publishes `facetime-remote-video` (1280×720, about 15 fps) and `facetime-remote-audio`.
- Frame timestamps advance about 1.01 s per sample, so frames are live, not repeated.
- Luminance tracks the camera: about 50–60 normal, about 12–20 with the lens covered. The caller
  reports the lens was only partly covered, which explains why it did not reach 0. Chert's docs
  warn that frames may include FaceTime UI or self-preview, so card recognition should still crop
  to the centre and require two consistent reads.
- On hang-up the track is muted, and the probe reports `no_current_frame` rather than an old frame.

## Next

Prompt 3: live staff-card recognition from these frames (one frame per second while awaiting a
card), using a vision model on the Gemini API free tier.
