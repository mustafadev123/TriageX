# TriageX — Devpost submission draft

**Category to select:** Best Use of FaceTime (Chert). Also the Healthcare track.

**Tagline:** Every ER capacity tool shows you the problem. TriageX calls the people who can fix it.

> **Synthetic data statement.** TriageX is a hackathon demo. Every hospital figure, patient, staff
> member and staff card is synthetic or fictional. Hospital names are used only for geographic
> context; no hospital supplied data to, approved or partnered with this project, and TriageX is
> not a clinically validated tool. No camera image is ever stored, and no phone number is stored.

## Inspiration

When a highway pileup sends a wave of ambulances to one emergency department, the dashboards
light up red, and then a charge nurse starts phoning people. Capacity tools are good at showing
the problem. The part that changes the outcome — getting qualified staff on the floor in time —
still runs on phone trees. We wanted the system that predicts the surge to also do the calling,
verify who answered, and show what that response is worth.

## What it does

1. **Predicts the surge.** A deterministic simulation of five Atlanta emergency departments runs
   an I-85 pileup scenario and forecasts each hospital's occupancy two hours ahead. TriageX
   proposes calling in staff where the forecast breaches capacity.
2. **A human approves.** Nothing is automatic. The approval starts a realistic 45-minute
   lead time.
3. **FaceTime does the verification.** A teammate FaceTimes the TriageX line. Our agent answers,
   tells them what is needed, and asks them to hold up their (fictional) staff card. It reads the
   card from the **live caller video**, and TriageX checks it against the roster: an unknown ID
   or an off-duty nurse is rejected out loud; a valid card is accepted only after the caller
   explicitly says "I confirm". The confirmation lands on the command-center dashboard within
   a fraction of a second.
4. **Two futures diverge.** The same patients, the same emergency: one future without TriageX,
   one with the confirmed staff arriving at 17:15. With three confirmations the demo window loses
   16.25 instead of 25.5 ambulance-hours to handoff delays (−36%), and the hospital recovers
   45 minutes sooner.
5. **iMessage and a grounded brief.** Staff can also confirm by replying IN to a "TriageX DEMO"
   iMessage, and an AI operational brief summarizes the situation using only numbers the
   simulation produced.

## How we built it

- **Simulation and API:** Python 3.12, FastAPI, Pydantic, a deterministic tick-based engine
  (seeded), WebSocket updates. A confirmation re-simulates only the TriageX future, so the
  baseline never moves.
- **Dashboard:** React, TypeScript, Vite, Tailwind, Recharts. The playhead reveals data only up
  to the current simulated time, so nothing is spoiled early.
- **Tiger Data:** PostgreSQL + TimescaleDB stores runs, per-tick hospital metrics (a hypertable),
  confirmations, FaceTime calls, card checks and briefs.
- **Chert FaceTime API + LiveKit:** Chert bridges a real FaceTime Video call into a LiveKit room;
  our Node agent (LiveKit Agents) speaks with **Gemini Live** and publishes an animated face.
- **Seeing the card:** the agent samples at most one caller frame per second, crops the centre
  (dropping FaceTime's controls and self-view), downsizes it to a grayscale image in memory, and
  asks **Gemini 3.5 Flash-Lite** for strict JSON (card present, ID, name, role, availability,
  confidence). A card counts only when two consecutive frames agree with confidence ≥ 0.8.
- **The backend decides.** The agent only extracts fields. FastAPI validates the card against a
  synthetic roster and records the confirmation exactly once, through the same code path as every
  other confirmation. "I confirm" is checked on the caller's own transcribed words, not on the
  voice model's judgement.
- **Chert iMessage:** the hackathon line's shared-messages API, polled while a reply is awaited.
- **Grounded AI brief:** Gemini writes text with `{metric_key}` placeholders only; the server
  fills in every number and rejects any digit or spelled-out number the model writes, with a
  fallback model and a deterministic template.

## Challenges

- **Free-tier latency and quotas.** Gemini answered in 1 s or in 50 s, and the free tier allows
  15 requests a minute per model. We pipeline requests, drop results overtaken by newer frames,
  and budget requests so the quota is never hit mid-call.
- **Temperature 0 was slow.** On some images it took 20–60 s; the default temperature took 1.5 s.
- **Not trusting the model.** Recognition needs two agreeing frames; validity is decided by the
  backend; confirmation needs the caller's actual words; the brief cannot write a number itself.
- **Video realities:** muted cameras, replaced tracks, glare, FaceTime UI in the frame, and
  stale frames — the agent never reuses an old frame.

## Accomplishments

- On a real FaceTime call the agent read five different cards with the exact ID and reported
  "no card" for a blank screen.
- The full card → roster → confirmation → dashboard path runs in well under a second after the
  confirmation; the baseline is provably unchanged.
- Hundreds of automated tests across the simulation, API, agent and dashboard.

## What we learned

Live video is a verification channel, not decoration — but only if every step that matters is
checked by deterministic code rather than by the model.

## What's next

- The agent places the call itself (outbound FaceTime) the moment an operator approves.
- QR codes on cards as a deterministic second signal.
- Real rosters and shift systems, with identity verification beyond a card.
- Patient-facing navigation (deliberately cut from this MVP).

## Built with

Python · FastAPI · Pydantic · uv · React · TypeScript · Vite · Tailwind CSS · Recharts ·
Tiger Data (PostgreSQL, TimescaleDB) · Chert FaceTime API · Chert iMessage · LiveKit Agents ·
Gemini Live · Gemini API (3.5 Flash-Lite vision, 3.1 Flash-Lite brief) · Playwright · Node.js
