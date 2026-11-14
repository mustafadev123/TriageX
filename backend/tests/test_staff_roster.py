"""The synthetic staff-card roster stays consistent with the fictional DEMO_STAFF."""

import json
from pathlib import Path

from app.services.demo_service import STAFF_NAMES

ROSTER = Path(__file__).resolve().parents[1] / "app" / "data" / "staff_roster.json"


def _staff() -> list[dict]:
    return json.loads(ROSTER.read_text(encoding="utf-8"))["staff"]


def test_roster_maps_card_ids_to_demo_staff() -> None:
    staff = _staff()
    card_ids = [s["card_id"] for s in staff]
    staff_ids = [s["staff_id"] for s in staff]
    assert len(set(card_ids)) == len(card_ids)
    assert len(set(staff_ids)) == len(staff_ids)
    for s in staff:
        assert s["staff_id"] in STAFF_NAMES
        assert s["name"] == STAFF_NAMES[s["staff_id"]]
        assert s["availability"] in {"AVAILABLE", "OFF DUTY"}
    by_card = {s["card_id"]: s["staff_id"] for s in staff}
    assert by_card["TX-RN-1001"] == "rn-avery"
    assert by_card["TX-RN-1002"] == "rn-jordan"
    assert by_card["TX-RN-1003"] == "rn-morgan"


def test_roster_has_off_duty_entry_and_no_unknown_test_card() -> None:
    staff = _staff()
    assert any(s["availability"] == "OFF DUTY" for s in staff)
    assert all(s["card_id"] != "TX-RN-9999" for s in staff)
