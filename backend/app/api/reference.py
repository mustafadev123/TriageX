from fastapi import APIRouter
from pydantic import BaseModel

from app.models.dashboard import HospitalInfo
from app.simulation.hospitals import DEFAULT_NETWORK
from app.simulation.scenarios import SCENARIOS

router = APIRouter(tags=["reference"])


class ScenarioInfo(BaseModel):
    id: str
    name: str
    description: str
    focus_hospital_id: str
    surge_onset_tick: int
    demo_window_start_tick: int
    demo_window_end_tick: int
    start_clock: str


@router.get("/hospitals")
def list_hospitals() -> list[HospitalInfo]:
    return [
        HospitalInfo(
            id=h.id,
            name=h.name,
            short_name=h.short_name,
            lat=h.lat,
            lon=h.lon,
            treatment_spaces=h.treatment_spaces,
            base_staff=h.base_staff,
        )
        for h in DEFAULT_NETWORK.hospitals
    ]


@router.get("/scenarios")
def list_scenarios() -> list[ScenarioInfo]:
    return [
        ScenarioInfo(
            id=s.id,
            name=s.name,
            description=s.description,
            focus_hospital_id=s.focus_hospital_id,
            surge_onset_tick=s.surge_onset_tick,
            demo_window_start_tick=s.demo_window[0],
            demo_window_end_tick=s.demo_window[1],
            start_clock=s.clock_label(0),
        )
        for s in SCENARIOS.values()
    ]
