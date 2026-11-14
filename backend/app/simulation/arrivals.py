"""Seeded synthetic patient generation.

All randomness in the simulation lives here. Patients are sampled once, before either
track runs, and the same immutable tuple is handed to both tracks.
"""

from __future__ import annotations

import hashlib
import math
import random

from app.simulation.hospitals import Network
from app.simulation.models import TICK_MINUTES, TICKS_PER_HOUR, Mode, Patient
from app.simulation.scenarios import AcuityMix, Scenario

WARMUP_TICKS = 32  # 8 simulated hours to reach a realistic starting occupancy

# Relative arrival intensity by clock hour (synthetic ED-like shape, mean ~0.92).
DIURNAL_PROFILE: tuple[float, ...] = (
    0.55, 0.48, 0.42, 0.40, 0.40, 0.45, 0.60, 0.80, 1.00, 1.15, 1.25, 1.30,
    1.32, 1.30, 1.28, 1.25, 1.22, 1.20, 1.18, 1.12, 1.05, 0.95, 0.80, 0.65,
)  # fmt: skip

WALK_IN_ACUITY_MIX: AcuityMix = (0.06, 0.44, 0.50)
AMBULANCE_ACUITY_MIX: AcuityMix = (0.28, 0.52, 0.20)
SERVICE_MEAN_MINUTES = {1: 300.0, 2: 200.0, 3: 100.0}
SERVICE_SIGMA = 0.45  # lognormal shape


def derive_seed(*parts: object) -> int:
    """Stable 64-bit seed from arbitrary parts (independent of PYTHONHASHSEED)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _choose_acuity(rng: random.Random, mix: AcuityMix) -> int:
    r = rng.random() * sum(mix)
    cumulative = 0.0
    for acuity, weight in enumerate(mix, start=1):
        cumulative += weight
        if r < cumulative:
            return acuity
    return 3


def _service_ticks(rng: random.Random, acuity: int, multiplier: float) -> int:
    mean = SERVICE_MEAN_MINUTES[acuity] * multiplier
    mu = math.log(mean) - SERVICE_SIGMA**2 / 2
    minutes = rng.lognormvariate(mu, SERVICE_SIGMA)
    return max(1, round(minutes / TICK_MINUTES))


def expected_arrivals_per_tick(base_per_hour: float, clock_hour: int, multiplier: float) -> float:
    return base_per_hour * DIURNAL_PROFILE[clock_hour] * multiplier / TICKS_PER_HOUR


def generate_patients(
    network: Network,
    scenario: Scenario,
    seed: int,
    warmup_ticks: int = WARMUP_TICKS,
) -> tuple[Patient, ...]:
    """Pre-sample every arrival from tick -warmup_ticks to the end of the horizon."""
    raw: list[tuple[int, int, int, Patient]] = []
    walk_mix = scenario.walk_in_acuity_mix or WALK_IN_ACUITY_MIX
    for h_index, hospital in enumerate(network.hospitals):
        rng = random.Random(derive_seed("triagex", seed, scenario.id, hospital.id))
        bursts = [b for b in scenario.bursts if b.hospital_id == hospital.id]
        amb_frac = min(
            1.0, max(0.0, hospital.ambulance_fraction + scenario.ambulance_fraction_delta)
        )
        for tick in range(-warmup_ticks, scenario.horizon_ticks):
            # (mode probability, acuity mix, source) for each arrival, in sampling order.
            draws: list[tuple[float, AcuityMix, AcuityMix, str]] = []
            lam = expected_arrivals_per_tick(
                hospital.base_arrivals_per_hour,
                scenario.clock_hour(tick),
                scenario.demand_multiplier(hospital.id, tick),
            )
            draws += [(amb_frac, AMBULANCE_ACUITY_MIX, walk_mix, "baseline")] * poisson(rng, lam)
            for burst in bursts:
                n = poisson(rng, burst.expected_at(tick))
                draws += [
                    (burst.ambulance_fraction, burst.acuity_mix, burst.acuity_mix, "surge")
                ] * n
            for k, (p_amb, amb_mix, walk_in_mix, source) in enumerate(draws):
                is_ambulance = rng.random() < p_amb
                mode = Mode.AMBULANCE if is_ambulance else Mode.WALK_IN
                acuity = _choose_acuity(rng, amb_mix if is_ambulance else walk_in_mix)
                service = _service_ticks(rng, acuity, scenario.service_multiplier)
                patient = Patient(
                    0, f"{hospital.id}:{tick}:{k}", hospital.id, tick, mode, acuity, service, source
                )
                raw.append((tick, h_index, k, patient))

    raw.sort(key=lambda item: item[:3])
    return tuple(
        Patient(
            seq, p.id, p.hospital_id, p.arrival_tick, p.mode, p.acuity, p.service_ticks, p.source
        )
        for seq, (*_, p) in enumerate(raw)
    )
