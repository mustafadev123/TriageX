-- Per-tick hospital state for both futures. Converted to a TimescaleDB hypertable on `ts`
-- by the migration runner when the timescaledb extension is available (Tiger Data).
--
-- The baseline track is stored once (series_version 0) and never rewritten. Each
-- recalculation of Track B is stored as a new series_version, so history is preserved.

CREATE TABLE IF NOT EXISTS hospital_metrics (
    ts                       timestamptz NOT NULL,
    run_id                   text NOT NULL REFERENCES simulation_runs ON DELETE CASCADE,
    track                    text NOT NULL CHECK (track IN ('baseline', 'triagex')),
    series_version           integer NOT NULL,
    hospital_id              text NOT NULL REFERENCES hospitals,
    tick                     integer NOT NULL,
    status                   text NOT NULL CHECK (status IN ('normal', 'pressure', 'breach')),
    saturation_pct           double precision NOT NULL,
    occupied                 integer NOT NULL,
    queue_length             integer NOT NULL,
    ambulances_waiting       integer NOT NULL,
    staff_on_duty            integer NOT NULL,
    staffed_capacity         integer NOT NULL,
    mean_wait_minutes        double precision NOT NULL,
    forecast_saturation_pct  double precision NOT NULL,
    diverting                boolean NOT NULL,
    diverted_out             integer NOT NULL,
    transfers_out            integer NOT NULL,
    PRIMARY KEY (run_id, track, series_version, hospital_id, ts)
);

CREATE INDEX IF NOT EXISTS hospital_metrics_run_series_ts_idx
    ON hospital_metrics (run_id, track, series_version, ts DESC);
