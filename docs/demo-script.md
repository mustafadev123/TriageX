# TriageX — 90-Second Live Demo Script

**One surge. Two futures. One confirmed response that changes the outcome.**

Every number on screen comes from the TriageX API (I-85 Multi-Vehicle Pileup, seed 42).
The presenter only moves the playhead and clicks the two explicit actions.

## Before going on stage

1. Start the backend:
   ```powershell
   cd backend
   uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
2. Start the frontend:
   ```powershell
   cd frontend
   npm run dev
   ```
   Then open http://localhost:5173.
3. Press **F** for fullscreen.
4. Click **Reset** (or press **Shift+R**). The clock reads **15:00**, the hero reads
   **0.00 h**, and the Next Event button reads **Start Surge**.
5. Optional: run `npm run rehearse -- rehearsal 1366 768` in `frontend/`. It walks the
   whole demo in Edge, checks the layout fits, and saves screenshots.

**Controls:**
- **Next Event:** the big teal button, **→**, or **Page Down**, which works with most
  presentation clickers.
- **Play/pause:** Space.
- **Step back one tick:** ←.
- **Technical view for judges:** D.

## Live FaceTime version (macOS, Prompts 4–6)

Four terminals (Node 24; never stop `chert dev` with anything but Ctrl+C):

```sh
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000      # 1. backend
cd frontend && npm run dev                                                   # 2. dashboard
cd agent && npm start                                                        # 3. agent
cd agent && npx --yes @trychert/facetime@0.1.0-beta.21 dev --agent triagex-facetime-agent --minutes 30   # 4. line
```

Needs `TRIAGEX_AGENT_TOKEN` (same value in `.env` and `agent/.env.local`) and, for the AI brief,
`GEMINI_API_KEY` in `.env`. The 16:30 steps change as follows:

1. **Approve outreach** at 16:30. The panel says "Waiting for a FaceTime call…".
2. The teammate FaceTime-videos the line. The agent names the request (Grady, staff needed,
   shift 17:15) and asks for the staff card.
3. Show **TX-RN-9999**: the agent reads it back and says it is not on the TriageX DEMO roster.
   The feed shows **Card Rejected**; the counter does not move.
4. Show **TX-RN-1001**: "on the roster"; the agent asks for "I confirm". Say it. The counter
   becomes 1/3 labeled **Live FaceTime**, with **Staff Confirmed via FaceTime** in the feed.
5. Then (and only then) click **Simulate staff reply "IN"** twice for the other two staff, and say
   they are simulated. Doing the simulated replies first would use Avery's fictional identity, and
   her card would then be refused as already confirmed.
6. Continue: **Activate Shift**, **Reveal Impact**. Optional: **Generate** the AI brief (bottom of
   the right column); its badge says Gemini, Gemini fallback, or Template.

If no FaceTime call arrives within 60 s of approval, or the call ends without a confirmation,
the panel says so and offers the iMessage reply (when configured) or the simulated fallback.

## The walkthrough

| Time | Presenter does | Screen shows | Say |
|---|---|---|---|
| 0–10 s | — | Calm network, two identical futures | "Five Atlanta EDs. Every capacity tool shows you *this*." |
| 10–20 s | **Next → Start Surge** | Clock runs to 16:00, Surge marker, Grady turns amber | "16:00, a pileup on I-85. Ambulances head for Grady." |
| 20–30 s | **Next → Show Prediction** | 16:15: Surge Predicted, dotted 2-hour forecast, card **Call in 3 staff at Grady** | "TriageX forecasts Grady over capacity within two hours, and proposes calling in staff." |
| 30–40 s | **Next** (moves to 16:30), then click **Approve outreach** | Outreach Approved 16:30 · 0/3 · shift starts 17:15; the 45-minute lead-time band appears | "A human approves. Nothing is automatic." |
| 40–55 s | Click **Simulate staff reply "IN"** three times | 1/3 → 2/3 → 3/3, Staff Confirmed in the feed, "Grady staffed capacity 45 → 56 from 17:15"; **the hero stays at 0.00** | "Staff confirm, but nothing changes yet: they need 45 minutes to arrive." |
| 55–65 s | **Next → Activate Shift** | 17:15: **SHIFT ACTIVE — GRADY**, Futures Diverge in the feed | "17:15, the shift starts. Same emergency, same patients, a different response." |
| 65–85 s | **Next → Reveal Impact** | The single line splits into two, the gap shades teal, the hero counts up to **9.25 h**, Back to Normal at 20:00 vs 20:45 | "Without TriageX, 25.5 ambulance-hours lost to handoff delays. With it, 16.25: 36% fewer. Grady recovers 45 minutes sooner." |
| 85–90 s | — | Final result | "Every ER capacity tool shows you the problem. TriageX calls the people who can fix it." |

Measured machine time for all six steps: about 14 seconds. The rest is narration.

## Things to say only if asked

- **The numbers:** they are synthetic, computed by a deterministic simulation. The final
  figures are only revealed when the playhead reaches them.
- **The replies:** they are a labeled simulated fallback. A real iMessage reply will
  update the same panel once the Chert integration (Phase 6) is verified.
- **Tiger Data:** press **D** for the persistence panel. It shows stored rows for this
  run and an hourly `time_bucket` query over the TimescaleDB hypertable.

## If something goes wrong

| Problem | What to do |
|---|---|
| Live reply does not arrive (Phase 6) | Click **Simulate staff reply "IN"**; the flow continues without a restart. |
| Backend error banner | Check the backend window, then click **Reset**. |
| Clicked past a step | Scrub the timeline back or press ←. Approval and confirmations stay recorded until Reset. |
| Pressed Next before approving | Nothing is approved automatically; the hint asks you to click **Approve outreach**. |
