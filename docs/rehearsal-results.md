# Rehearsal results

Handoff Prompt 7 asks for **three consecutive successful live runs** (real FaceTime call, invalid
card rejected in at least one run, valid card + "I confirm", dashboard update, divergence at 17:15,
impact read from the API), each about 90 seconds. Fill the live table with the output of
`REHEARSE_CONFIRM=live npm run rehearse -- <dir> 1366 768` (it writes `summary-1366x768.json`)
plus the `chert dev` call records. Name every fallback used.

## Live runs

| Run | Date/time (ET) | Total time | Call connect (incoming → bridge_ready) | Card read time | Confirm → dashboard | Outcome | Fallbacks used | Trial minutes left |
|---|---|---|---|---|---|---|---|---|
| 1 | | | | | | | | |
| 2 | | | | | | | | |
| 3 | | | | | | | | |

## Automated checks (no phone; not live runs)

| Date/time (ET) | Mode | Result |
|---|---|---|
| 2026-11-14 14:20 | `REHEARSE_CONFIRM=agent`, 1366×768, Chrome, backend without database | Agent API: TX-RN-9999 → `not_on_roster`, TX-RN-1001 → `valid`, confirm → dashboard 1/3 **Live FaceTime** after 90 ms; two labeled simulated replies 148 / 96 ms; 17:15 shift active; final hero **9.25 h** = API `impact.primary.delta` −9.25; reset back to 15:00 / 0.00; layout fits without scrolling; no console errors. AI brief at 16:30: **Gemini fallback** (primary slower than 8 s), 10.4 s |

Earlier live evidence for the parts of the flow: [chert-status.md](chert-status.md) (calls 1–4).
