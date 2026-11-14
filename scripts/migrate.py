"""Apply TriageX database migrations to DATABASE_URL.

    cd backend
    uv run python ../scripts/migrate.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.repositories.migrations import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
