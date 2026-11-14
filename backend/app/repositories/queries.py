"""Read queries over persisted runs, used for the Tiger Data sponsor demonstration.

Every result comes from rows stored in the database. Queries pick the baseline series
(version 0) and the latest stored Track B series, and use TimescaleDB's `time_bucket`
when the extension is present (PostgreSQL's `date_bin` otherwise).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.repositories.persistence import SIM_TZ, tick_time
from app.simulation.scenarios import Scenario

LATEST_SERIES = """
    latest AS (
        SELECT triagex_series_version AS sv FROM run_versions
        WHERE run_id = :run_id ORDER BY version DESC LIMIT 1
    ),
    series AS (
        SELECT m.* FROM hospital_metrics m, latest
        WHERE m.run_id = :run_id
          AND ((m.track = 'baseline' AND m.series_version = 0)
               OR (m.track = 'triagex' AND m.series_version = latest.sv))
    )"""


def bucket_expr(timescale: bool, column: str = "ts") -> str:
    if timescale:
        return f"time_bucket(INTERVAL '1 hour', {column})"
    return f"date_bin(INTERVAL '1 hour', {column}, TIMESTAMPTZ '2000-01-01')"


def queries(timescale: bool) -> dict[str, str]:
    bucket = bucket_expr(timescale)
    return {
        "hourly_focus_comparison": f"""
WITH {LATEST_SERIES}
SELECT {bucket} AS hour, track,
       round(avg(saturation_pct)::numeric, 1)          AS avg_saturation_pct,
       max(saturation_pct)                             AS peak_saturation_pct,
       round(sum(ambulances_waiting) * 15 / 60.0, 2)   AS ambulance_hours_waiting,
       max(staff_on_duty)                              AS max_staff_on_duty
FROM series
WHERE hospital_id = :focus AND ts >= :window_start AND ts < :window_end
GROUP BY 1, 2 ORDER BY 1, 2""",
        "ambulance_handoff_by_hospital": f"""
WITH {LATEST_SERIES}
SELECT hospital_id,
       round(sum(ambulances_waiting) FILTER (WHERE track = 'baseline') * 15 / 60.0, 2)
           AS baseline_ambulance_hours,
       round(sum(ambulances_waiting) FILTER (WHERE track = 'triagex') * 15 / 60.0, 2)
           AS triagex_ambulance_hours,
       count(*) FILTER (WHERE track = 'baseline' AND status = 'breach') AS baseline_breach_ticks,
       count(*) FILTER (WHERE track = 'triagex' AND status = 'breach')  AS triagex_breach_ticks
FROM series
WHERE ts >= :window_start AND ts < :window_end
GROUP BY hospital_id ORDER BY baseline_ambulance_hours DESC, hospital_id""",
        "intervention_timeline": f"""
WITH {LATEST_SERIES}
SELECT e.kind, e.tick, e.sim_time, e.wall_time,
       b.saturation_pct AS baseline_saturation_pct,
       t.saturation_pct AS triagex_saturation_pct,
       t.staff_on_duty  AS triagex_staff_on_duty,
       e.detail
FROM intervention_events e
LEFT JOIN series b ON b.track = 'baseline' AND b.hospital_id = e.hospital_id AND b.ts = e.sim_time
LEFT JOIN series t ON t.track = 'triagex'  AND t.hospital_id = e.hospital_id AND t.ts = e.sim_time
WHERE e.run_id = :run_id
ORDER BY e.tick, e.kind""",
        "focus_recovery": f"""
WITH {LATEST_SERIES},
focus AS (
    SELECT track, ts, status FROM series
    WHERE hospital_id = :focus AND ts >= :onset
),
calm AS (
    SELECT track, ts,
           bool_and(status = 'normal') OVER w AS calm_hour,
           count(*) OVER w AS ticks_ahead
    FROM focus
    WINDOW w AS (PARTITION BY track ORDER BY ts ROWS BETWEEN CURRENT ROW AND 3 FOLLOWING)
),
first_breach AS (
    SELECT track, min(ts) AS first_breach_at FROM focus WHERE status = 'breach' GROUP BY track
)
SELECT f.track, f.first_breach_at,
       min(c.ts) AS back_to_normal_at,
       (SELECT count(*) FROM focus x WHERE x.track = f.track AND x.status = 'breach') * 15 / 60.0
           AS breach_hours
FROM first_breach f
LEFT JOIN calm c ON c.track = f.track AND c.ts > f.first_breach_at
                AND c.calm_hour AND c.ticks_ahead = 4
GROUP BY f.track, f.first_breach_at ORDER BY f.track""",
        "confirmation_impact_history": """
SELECT v.version, v.reason, v.recorded_at, v.confirmed_staff, v.recompute_ms,
       c.baseline AS baseline_ambulance_hours, c.triagex AS triagex_ambulance_hours,
       c.delta, c.pct_change
FROM run_versions v
LEFT JOIN comparison_metrics c ON c.run_id = v.run_id AND c.version = v.version
     AND c.scope = 'hero' AND c.metric = 'ambulance_handoff_hours'
WHERE v.run_id = :run_id
ORDER BY v.version""",
        "stored_row_counts": """
SELECT 'hospital_metrics' AS table_name, count(*) AS row_count
       FROM hospital_metrics WHERE run_id = :run_id
UNION ALL SELECT 'run_versions', count(*) FROM run_versions WHERE run_id = :run_id
UNION ALL SELECT 'comparison_metrics', count(*) FROM comparison_metrics WHERE run_id = :run_id
UNION ALL SELECT 'staffing_requests', count(*) FROM staffing_requests WHERE run_id = :run_id
UNION ALL SELECT 'staff_confirmations', count(*) FROM staff_confirmations c
          JOIN staffing_requests r USING (request_id) WHERE r.run_id = :run_id
UNION ALL SELECT 'intervention_events', count(*) FROM intervention_events WHERE run_id = :run_id
UNION ALL SELECT 'agent_activity', count(*) FROM agent_activity WHERE run_id = :run_id
UNION ALL SELECT 'outreach_events', count(*) FROM outreach_events WHERE run_id = :run_id
UNION ALL SELECT 'webhook_events', count(*) FROM webhook_events WHERE run_id = :run_id""",
        "messaging_events": """
SELECT 'outreach' AS kind, o.created_at AS at, o.recipient_mask AS who, o.status AS outcome,
       o.send_ms AS ms
FROM outreach_events o WHERE o.run_id = :run_id
UNION ALL
SELECT 'webhook', w.received_at, coalesce(o.recipient_mask, '—'), w.outcome, w.processing_ms
FROM webhook_events w LEFT JOIN outreach_events o USING (outreach_id)
WHERE w.run_id = :run_id
ORDER BY at""",
    }


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        # Show stored instants on the simulation's clock (America/New_York).
        return value.astimezone(SIM_TZ).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def params_for(run_id: str, scenario: Scenario) -> dict:
    start, end = scenario.demo_window
    return {
        "run_id": run_id,
        "focus": scenario.focus_hospital_id,
        "window_start": tick_time(scenario, start),
        "window_end": tick_time(scenario, end),
        "onset": tick_time(scenario, scenario.surge_onset_tick),
    }


def run_query(con, sql: str, params: dict) -> dict:
    used = {k: v for k, v in params.items() if f":{k}" in sql}
    rows = con.run(sql, **used)
    columns = [c["name"] for c in con.columns] if con.columns else []
    return {
        "sql": sql.strip(),
        "columns": columns,
        "rows": [[_jsonable(v) for v in row] for row in rows],
    }


def run_insights(db, run_id: str, scenario: Scenario, timescale: bool) -> dict[str, dict]:
    params = params_for(run_id, scenario)
    with db.connection() as con:
        return {name: run_query(con, sql, params) for name, sql in queries(timescale).items()}
