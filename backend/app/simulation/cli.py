"""Command-line comparison runner.

uv run python -m app.simulation.cli i85_pileup --seed 42
uv run python -m app.simulation.cli i85_pileup --seed 42 --staff 0 --no-level-loading
uv run python -m app.simulation.cli heat_wave --json > out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from app.simulation.comparison import Comparison, _jsonable, run_comparison
from app.simulation.hospitals import DEFAULT_NETWORK
from app.simulation.interventions import interventions_from_proposal
from app.simulation.metrics import LOWER_IS_BETTER, summarize
from app.simulation.models import Proposal
from app.simulation.scenarios import SCENARIOS, get_scenario

SHOWN = (
    "mean_wait_minutes", "p90_wait_minutes", "peak_saturation_pct", "hospital_ticks_in_breach",
    "total_queue_patient_hours", "mean_handoff_minutes", "ambulance_handoff_hours",
    "ambulance_hours_lost", "diversion_onsets", "diverted_ambulances", "transfers",
    "mean_staff_utilization_pct",
)  # fmt: skip


def pick_surge_proposal(proposals: list[Proposal], onset_tick: int) -> Proposal | None:
    """First staffing proposal at or after the scenario's surge onset."""
    for p in proposals:
        if p.kind == "staffing" and p.tick >= onset_tick:
            return p
    return None


def _table(title: str, deltas: dict) -> list[str]:
    lines = [title, f"  {'metric':30s} {'baseline':>10s} {'triagex':>10s} {'change':>10s}"]
    for name in SHOWN:
        d = deltas[name]
        pct = "n/a" if d.pct_change is None else f"{d.pct_change:+.1f}%"
        better = ""
        if d.delta and name in LOWER_IS_BETTER:
            better = " (better)" if d.delta < 0 else " (worse)"
        lines.append(f"  {name:30s} {d.baseline:10.2f} {d.triagex:10.2f} {pct:>10s}{better}")
    return lines


def render(c: Comparison, elapsed_ms: float) -> str:
    sc = c.scenario
    ws, we = sc.demo_window
    out = [
        "TriageX simulation - SYNTHETIC DATA, not real-time hospital data",
        f"Scenario: {sc.name}  seed={c.seed}  horizon={sc.clock_label(0)}-"
        f"{sc.clock_label(sc.horizon_ticks)}  patients sampled={len(c.patients)}",
        f"Demo window: {sc.clock_label(ws)}-{sc.clock_label(we)}  run time {elapsed_ms:.0f} ms",
        "",
        "Interventions:",
    ]
    if not c.triagex.interventions:
        out.append("  none (Track B identical to baseline)")
    for e in c.intervention_effects:
        line = (
            f"  {e.kind:13s} {e.hospital_id:14s} approved {sc.clock_label(e.approved_tick)}, "
            f"active {sc.clock_label(e.effective_tick)}-{sc.clock_label(e.end_tick)}"
        )
        if e.kind == "staffing":
            line += (
                f", confirmed {e.confirmed_staff} (usable {e.usable_staff}, blocked by space "
                f"{e.unusable_staff}), capacity {e.staffed_capacity_before}->"
                f"{e.staffed_capacity_with_staff}"
            )
        else:
            line += f", redirects executed {e.redirects_executed}"
        out.append(line)
    div = c.first_divergence_tick
    out.append(f"First divergence: {'none' if div is None else sc.clock_label(div)}")
    out.append("")
    out += _table("Demo window (patients arriving in window):", c.window_deltas)
    out.append("")
    out += _table("Full 24-hour horizon:", c.deltas)
    out.append("")
    out.append("Peak saturation by hospital (baseline -> triagex):")
    for hid in c.baseline.hospital_ids:
        a, b = summarize(c.baseline, hid), summarize(c.triagex, hid)
        out.append(
            f"  {DEFAULT_NETWORK.get(hid).short_name:14s} {a.peak_saturation_pct:7.1f}% "
            f"-> {b.peak_saturation_pct:7.1f}%   diverted {a.diverted_ambulances}->"
            f"{b.diverted_ambulances}"
        )
    out.append("")
    out.append("Timeline markers:")
    for m in c.markers:
        if ws <= m.tick <= we or m.kind in ("approval", "shift_active", "divergence"):
            where = f" [{m.hospital_id}]" if m.hospital_id else ""
            out.append(f"  {m.clock:>12s}  {m.kind:20s} {m.label}{where}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a TriageX baseline-vs-TriageX comparison.")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--staff",
        type=int,
        default=None,
        help="confirmed staff (default: the proposal's recommendation)",
    )
    parser.add_argument(
        "--approve-delay", type=int, default=1, help="ticks between proposal and operator approval"
    )
    parser.add_argument("--no-level-loading", action="store_true")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    scenario = get_scenario(args.scenario)
    interventions: tuple = ()
    if not args.baseline_only:
        probe = run_comparison(scenario, args.seed)
        proposal = pick_surge_proposal(probe.baseline.proposals, scenario.surge_onset_tick)
        if proposal is None:
            print(
                "No surge-time staffing proposal was raised; running without interventions.",
                file=sys.stderr,
            )
        else:
            staff = proposal.recommended_staff if args.staff is None else args.staff
            interventions = interventions_from_proposal(
                proposal,
                proposal.tick + args.approve_delay,
                staff,
                include_level_loading=not args.no_level_loading,
            )

    started = time.perf_counter()
    comparison = run_comparison(scenario, args.seed, interventions)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if args.json:
        print(json.dumps(_jsonable(comparison), default=str, indent=2))
    else:
        print(render(comparison, elapsed_ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
