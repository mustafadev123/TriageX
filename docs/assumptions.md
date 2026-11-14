# TriageX Simulation Assumptions

> **Synthetic hospital operations simulation — not real-time hospital data.**
> Hospital names and approximate coordinates are used only for map context. Every
> capacity, staffing, demand, and outcome figure is synthetic and describes no real
> hospital. No hospital supplied data to, approved, or partnered with this project.

This document lists every modeling assumption in the Phase 2 engine
(`backend/app/simulation/`), so results can be read for what they are: the output of a
simple, transparent model, not a validated prediction.

## Time

- One tick is 15 simulated minutes. The horizon is 96 ticks (24 hours).
- Each scenario has a 24-tick (6-hour) demo window covering its surge.
- Wait and handoff times therefore have 15-minute resolution.
- Before tick 0, 32 ticks (8 hours) of ordinary demand are simulated so hospitals start
  partly occupied. This warm-up is identical in both tracks and excluded from metrics.

## Hospitals (all values synthetic)

| Hospital | Treatment spaces | Base staff | Staffed capacity | Base arrivals/hour |
|---|---|---|---|---|
| Grady Memorial | 56 | 13 | 45 | 9.5 |
| Emory University Hospital | 40 | 9 | 31 | 6.5 |
| Emory University Hospital Midtown | 44 | 10 | 35 | 7.5 |
| Piedmont Atlanta | 42 | 10 | 35 | 7.0 |
| Wellstar Kennestone | 52 | 12 | 42 | 9.0 |

- **Staffed capacity** = `min(treatment spaces, floor(staff × 3.5))`. Extra staff never
  create physical space. For example, Grady can use at most 3 extra staff (45 → 56).
- Staff are "nurse-equivalents" with a constant roster; shift changes are not modeled.
- Separate surge spaces (hallway beds) from the Phase 0 plan are not modeled.

## Demand

- Arrivals per hospital per tick are Poisson with rate
  `base × diurnal(clock hour) × scenario multiplier`.
- The diurnal profile is synthetic (low overnight, peak around midday).
- 20–26% of ordinary arrivals come by ambulance.
- Acuity is 1 (most urgent) to 3. Walk-ins: 6% / 44% / 50%. Ambulance: 28% / 52% / 20%.
- Treatment time is lognormal by acuity (means of 300, 200 and 100 minutes; σ = 0.45),
  scaled by the scenario's service multiplier.
- Every patient is sampled once, before either track runs. Both tracks receive the same
  immutable patient tuple.

## Queue and service

- Each hospital has one queue, ordered by acuity, then ED arrival tick, then a stable
  sequence number.
- A patient occupies one staffed space for their whole treatment time.
- If staffing drops below current occupancy, nobody is removed; new placements simply
  wait.
- **Saturation** = `(patients in treatment + patients waiting) / treatment spaces`.
  Status is normal below 85%, pressure from 85% to just under 100%, and breach at 100%
  or more.

## Ambulance handoff and diversion (both tracks)

- An ambulance crew is held from ED arrival until its patient is placed. That interval
  is the handoff wait.
- A hospital goes on diversion after 2 consecutive ticks at saturation of 100% or more
  with at least one ambulance waiting. It comes off diversion below 90%.
- While a hospital is diverting, non-critical ambulances (acuity 2–3) go to the nearest
  hospital that is not diverting. Acuity-1 ambulances are never diverted. If every
  hospital is diverting, ambulances continue to their intended hospital.
- Reroute travel = 5 minutes plus straight-line distance × 1.3 at 45 km/h, rounded up to
  whole ticks (at least one).
- **Ambulance-hours lost** = handoff waiting hours plus extra reroute travel hours.

## Forecast

- The forecast projects saturation two hours ahead with a fluid model, using only an
  `ObservedState`: current occupancy, queue, and staffed capacity; arrivals in the
  trailing hour; the ordinary diurnal shape; and a fixed 170-minute length-of-stay
  prior.
- It does not see the scenario definition or any future arrival. A test replaces all
  future arrivals and checks that earlier forecasts do not change.
- The forecast is simple and noisy. Across 5 seeds × 4 scenarios, its error at 2 hours is
  roughly unbiased (−6 to +2 percentage points) with a mean absolute error of 11–14
  points. During a sudden burst it overshoots, because it cannot know the burst will end.
- **Proposal rule:** when the forecast reaches 90% or more, the engine raises a staffing
  proposal (if extra staff can still add capacity) and a level-loading proposal. It
  re-arms only after the forecast drops below 80%.
- False alarms happen. For example, Grady can trip the threshold during ordinary midday
  peaks.

## TriageX interventions (Track B only, human-approved inputs)

- **Staffing**
  - Each confirmation adds one staff member.
  - Effect starts 45 simulated minutes (3 ticks) after the operator approves the
    request, and lasts 4 hours (16 ticks).
  - A confirmation's simulated effect never depends on when the real reply arrives.
  - Capacity is still capped by treatment spaces.
- **Level-loading**
  - Starts 15 minutes after approval and lasts 4 hours.
  - Up to 2 acuity-3 ambulances per tick are sent to the nearest hospital that is not
    diverting, whose forecast at the previous tick was below 80%, and that has accepted
    fewer than 2 redirects that tick.
  - If the source hospital is already diverting, level-loading only chooses the receiver
    using forecasts, and the patient still counts as diverted.
  - When it redirects patients from a hospital that is not diverting, these count as
    transfers.
- With no interventions, Track B is identical to Track A. Tests check this.

## Metrics

- Patient metrics cover patients whose ED arrival falls inside the measured range.
- Patients still waiting at the end of the horizon contribute a censored wait (horizon
  end minus arrival), so long queues are never dropped.
- Percentiles use the nearest-rank method.
- Percent changes are computed from the two tracks' values. None are hardcoded.

## Scenarios

| Scenario | Tick 0 | Demo window | Main assumption |
|---|---|---|---|
| I-85 Multi-Vehicle Pileup | 12:00 | 15:00–21:00 | 67 extra, mostly high-acuity ambulance patients from 16:00, front-loaded over 2 hours; 42 expected at Grady |
| Big Game Downtown | 12:00 | 21:00–03:00 | Demand up to 2.5× at Grady and 2.2× at Midtown from 22:00, plus 44 crowd-injury arrivals |
| Summer Heat Wave | 08:00 | 13:00–19:00 | Metro-wide demand up to 1.4× from 13:00 to 20:00, 10% longer treatment times, more ambulances |
| Respiratory Illness Wave | 06:00 | 16:00–22:00 | 1.15× all day (about 1.32× in the evening), 10% longer treatment times |

## Robustness (20 seeds each)

The demo intervention is the first surge-time staffing proposal: the recommended staff,
plus level-loading, approved 15 minutes after the proposal. These figures are demo-window
changes, Track B minus Track A.

| Scenario | Mean wait change (median, range) | Ambulance-hours change (median, range) | Seeds where TriageX was worse |
|---|---|---|---|
| I-85 Pileup | −11.3 min (−16.6 to −3.4) | −17.6 h (−35.8 to −3.2) | none on wait or ambulance-hours; 1 on diversions |
| Big Game | −5.8 min (−18.0 to −0.4) | −3.6 h (−19.5 to +0.2) | 1 on ambulance-hours |
| Heat Wave | −6.8 min (−14.1 to +0.1) | −2.9 h (−12.2 to +2.0) | 1 on wait; 5 on ambulance-hours; 2 on diversions |
| Respiratory Wave | 0.0 min (−11.5 to +0.2) | 0.0 h (−2.2 to 0.0) | 2 on wait |

Respiratory Wave shows little effect. Its first proposal often comes from a hospital
that is not the binding constraint, and demand stays high for longer than the 4-hour
shift. That is a model result, not a bug.

## Known limitations

- Not clinically validated. There is no boarding, inpatient bed flow, lab or imaging
  delay, or left-without-being-seen behavior.
- Staffing is a single pooled number per hospital.
- Level-loading assumes EMS complies immediately.
- Determinism is exact on a given platform and Python version. Floating-point library
  differences across platforms could change results.
