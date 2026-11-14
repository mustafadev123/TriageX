"""Export real API responses for the I-85 demo as frontend test fixtures.

The fixtures are produced by the actual FastAPI app and simulation engine (in memory,
no database), so frontend tests exercise genuine backend data rather than hand-made
numbers. Re-run after any backend change that alters the payload:

    cd backend
    uv run python ../scripts/export_fixtures.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.repositories.persistence import PersistenceWriter  # noqa: E402
from app.services.demo_service import DemoService  # noqa: E402

OUT = ROOT / "frontend" / "src" / "test" / "fixtures"
VOLATILE = ("run_id", "created_at", "generated_at", "approved_at", "expires_at", "received_at",
            "wall_time", "recompute_ms", "build_ms")  # fmt: skip


def scrub(value):
    """Make fixtures stable across exports (ids and wall-clock times only)."""
    if isinstance(value, dict):
        return {k: ("<volatile>" if k in VOLATILE and v is not None else scrub(v))
                for k, v in value.items()}  # fmt: skip
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str) and value.startswith("req-"):
        return "req-fixture"
    return value


def main() -> int:
    app = create_app(DemoService(), persistence=PersistenceWriter(None))
    OUT.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client:
        states = {"initial": client.post("/api/demo/reset").json()}
        run_id = states["initial"]["run"]["run_id"]
        card = next(p for p in states["initial"]["proposals"] if p["recommended"])
        states["approved"] = client.post(
            f"/api/runs/{run_id}/proposals/{card['id']}/approve",
            json={"at_tick": card["tick"] + 1},
        ).json()
        request_id = states["approved"]["staffing_requests"][0]["request_id"]
        for n in (1, 2, 3):
            states[f"confirmed{n}"] = client.post(
                f"/api/runs/{run_id}/staffing-requests/{request_id}/simulate-reply"
            ).json()
    for name in ("initial", "approved", "confirmed1", "confirmed2", "confirmed3"):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(scrub(states[name]), separators=(",", ":")), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
