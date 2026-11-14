"""Tiger Data row for an AI operational brief (written by PersistenceWriter)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.services.brief import Brief

_SQL = """
INSERT INTO ai_briefs (brief_id, run_id, run_version, at_tick, clock, provider, model,
    latency_ms, fallback_reason, headline, bullets, action, metric_keys, generated_at)
VALUES (:brief_id, :run_id, :run_version, :at_tick, :clock, :provider, :model, :latency_ms,
    :fallback_reason, :headline, CAST(:bullets AS jsonb), :action, CAST(:metric_keys AS text[]),
    CAST(:generated_at AS timestamptz))
ON CONFLICT (brief_id) DO NOTHING
"""


@dataclass(slots=True, frozen=True)
class BriefRow:
    params: dict
    run_id: str
    version: str = "ai_brief"

    @classmethod
    def of(cls, b: Brief) -> BriefRow:
        return cls(
            {
                "brief_id": b.brief_id,
                "run_id": b.run_id,
                "run_version": b.version,
                "at_tick": b.at_tick,
                "clock": b.clock,
                "provider": b.provider,
                "model": b.model,
                "latency_ms": b.latency_ms,
                "fallback_reason": b.fallback_reason,
                "headline": b.headline,
                "bullets": json.dumps(b.bullets),
                "action": b.action,
                "metric_keys": b.metric_keys,
                "generated_at": b.generated_at.isoformat(),
            },
            b.run_id,
        )

    def write(self, con) -> int:
        con.run(_SQL, **self.params)
        return 1
