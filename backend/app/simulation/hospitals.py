"""The five-hospital Metro Atlanta network.

Names and approximate coordinates are real and used only for map context. Every
capacity, staffing, and demand figure is SYNTHETIC and does not describe the real
hospitals.
"""

from __future__ import annotations

import math

from app.simulation.models import TICK_MINUTES, Hospital

HOSPITALS: tuple[Hospital, ...] = (
    Hospital(
        id="grady",
        name="Grady Memorial Hospital",
        short_name="Grady",
        lat=33.7522,
        lon=-84.3818,
        treatment_spaces=56,
        base_staff=13,
        patients_per_staff=3.5,
        base_arrivals_per_hour=9.5,
        ambulance_fraction=0.26,
        expected_service_minutes=170,
    ),
    Hospital(
        id="emory_uh",
        name="Emory University Hospital",
        short_name="Emory UH",
        lat=33.7930,
        lon=-84.3200,
        treatment_spaces=40,
        base_staff=9,
        patients_per_staff=3.5,
        base_arrivals_per_hour=6.5,
        ambulance_fraction=0.20,
        expected_service_minutes=170,
    ),
    Hospital(
        id="emory_midtown",
        name="Emory University Hospital Midtown",
        short_name="Emory Midtown",
        lat=33.7689,
        lon=-84.3866,
        treatment_spaces=44,
        base_staff=10,
        patients_per_staff=3.5,
        base_arrivals_per_hour=7.5,
        ambulance_fraction=0.22,
        expected_service_minutes=170,
    ),
    Hospital(
        id="piedmont",
        name="Piedmont Atlanta Hospital",
        short_name="Piedmont",
        lat=33.8090,
        lon=-84.3943,
        treatment_spaces=42,
        base_staff=10,
        patients_per_staff=3.5,
        base_arrivals_per_hour=7.0,
        ambulance_fraction=0.22,
        expected_service_minutes=170,
    ),
    Hospital(
        id="kennestone",
        name="Wellstar Kennestone Hospital",
        short_name="Kennestone",
        lat=33.9722,
        lon=-84.5528,
        treatment_spaces=52,
        base_staff=12,
        patients_per_staff=3.5,
        base_arrivals_per_hour=9.0,
        ambulance_fraction=0.24,
        expected_service_minutes=170,
    ),
)

# Ambulance travel model used for reroutes (assumption, see docs/assumptions.md).
ROAD_FACTOR = 1.3
AMBULANCE_SPEED_KMH = 45.0
HANDLING_MINUTES = 5.0


def haversine_km(a: Hospital, b: Hospital) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def travel_minutes(a: Hospital, b: Hospital) -> float:
    if a.id == b.id:
        return 0.0
    return HANDLING_MINUTES + haversine_km(a, b) * ROAD_FACTOR / AMBULANCE_SPEED_KMH * 60


def travel_ticks(a: Hospital, b: Hospital) -> int:
    """Extra ticks an ambulance spends when rerouted from a to b (at least one)."""
    if a.id == b.id:
        return 0
    return max(1, math.ceil(travel_minutes(a, b) / TICK_MINUTES))


class Network:
    """Hospitals plus precomputed travel times and nearest-neighbour orderings."""

    def __init__(self, hospitals: tuple[Hospital, ...]):
        ids = [h.id for h in hospitals]
        if len(set(ids)) != len(ids):
            raise ValueError("Hospital ids must be unique")
        self.hospitals = hospitals
        self.ids: tuple[str, ...] = tuple(ids)
        self.index: dict[str, int] = {h.id: i for i, h in enumerate(hospitals)}
        n = len(hospitals)
        self.travel: list[list[int]] = [
            [travel_ticks(hospitals[i], hospitals[j]) for j in range(n)] for i in range(n)
        ]
        # Other hospitals ordered by travel distance, ties broken by network order.
        self.by_distance: list[list[int]] = [
            sorted(
                (j for j in range(n) if j != i),
                key=lambda j, i=i: (haversine_km(hospitals[i], hospitals[j]), j),
            )
            for i in range(n)
        ]

    def get(self, hospital_id: str) -> Hospital:
        return self.hospitals[self.index[hospital_id]]


DEFAULT_NETWORK = Network(HOSPITALS)
