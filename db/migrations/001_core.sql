-- TriageX core schema. All operational values are SYNTHETIC simulation output.

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id         text PRIMARY KEY,
    name                text NOT NULL,
    short_name          text NOT NULL,
    lat                 double precision NOT NULL,
    lon                 double precision NOT NULL,
    treatment_spaces    integer NOT NULL CHECK (treatment_spaces > 0),
    base_staff          integer NOT NULL CHECK (base_staff >= 0),
    patients_per_staff  double precision NOT NULL CHECK (patients_per_staff > 0),
    is_synthetic        boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id             text PRIMARY KEY,
    name                    text NOT NULL,
    description             text NOT NULL,
    start_clock_minutes     integer NOT NULL,
    surge_onset_tick        integer NOT NULL,
    demo_window_start_tick  integer NOT NULL,
    demo_window_ticks       integer NOT NULL,
    horizon_ticks           integer NOT NULL,
    focus_hospital_id       text NOT NULL REFERENCES hospitals,
    assumptions             jsonb NOT NULL,
    definition_sha256       text NOT NULL
);

CREATE TABLE IF NOT EXISTS simulation_runs (
    run_id            text PRIMARY KEY,
    scenario_id       text NOT NULL REFERENCES scenarios,
    seed              bigint NOT NULL,
    created_at        timestamptz NOT NULL,
    sim_start         timestamptz NOT NULL,   -- synthetic calendar time of tick 0
    tick_minutes      integer NOT NULL,
    patients_sampled  integer NOT NULL,
    baseline_digest   text NOT NULL,          -- SHA-256 of the stored baseline tick series
    engine_version    text NOT NULL
);
CREATE INDEX IF NOT EXISTS simulation_runs_scenario_seed_idx ON simulation_runs (scenario_id, seed);

-- One row per change to a run's state (creation, approval, each confirmation).
CREATE TABLE IF NOT EXISTS run_versions (
    run_id                  text NOT NULL REFERENCES simulation_runs ON DELETE CASCADE,
    version                 integer NOT NULL,
    reason                  text NOT NULL,
    recorded_at             timestamptz NOT NULL,
    triagex_series_version  integer NOT NULL,  -- which stored Track B series this version shows
    triagex_digest          text NOT NULL,     -- SHA-256 of the stored Track B tick series
    confirmed_staff         integer NOT NULL,
    recompute_ms            double precision,
    PRIMARY KEY (run_id, version)
);

-- Computed comparison metrics exactly as returned to the dashboard for that version.
CREATE TABLE IF NOT EXISTS comparison_metrics (
    run_id      text NOT NULL,
    version     integer NOT NULL,
    scope       text NOT NULL CHECK (scope IN ('demo_window', 'full_horizon', 'hero')),
    metric      text NOT NULL,
    baseline    double precision NOT NULL,
    triagex     double precision NOT NULL,
    delta       double precision NOT NULL,
    pct_change  double precision,
    PRIMARY KEY (run_id, version, scope, metric),
    FOREIGN KEY (run_id, version) REFERENCES run_versions ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS staffing_requests (
    request_id       text PRIMARY KEY,
    run_id           text NOT NULL REFERENCES simulation_runs ON DELETE CASCADE,
    proposal_id      text NOT NULL,
    hospital_id      text NOT NULL REFERENCES hospitals,
    requested_staff  integer NOT NULL,
    proposal_tick    integer NOT NULL,
    approved_tick    integer NOT NULL CHECK (approved_tick >= proposal_tick),
    effective_tick   integer NOT NULL CHECK (effective_tick >= approved_tick),
    end_tick         integer NOT NULL,
    approved_at      timestamptz NOT NULL,
    expires_at       timestamptz NOT NULL,
    operator         text NOT NULL
);
CREATE INDEX IF NOT EXISTS staffing_requests_run_idx ON staffing_requests (run_id);

-- One confirmation per staff member per request (duplicate replies cannot double count).
CREATE TABLE IF NOT EXISTS staff_confirmations (
    request_id    text NOT NULL REFERENCES staffing_requests ON DELETE CASCADE,
    staff_id      text NOT NULL,
    display_name  text NOT NULL,
    source        text NOT NULL CHECK (source IN ('simulated', 'webhook')),
    received_at   timestamptz NOT NULL,
    recompute_ms  double precision,
    PRIMARY KEY (request_id, staff_id)
);

-- Approval, activation, and end of each intervention, in simulated and wall-clock time.
CREATE TABLE IF NOT EXISTS intervention_events (
    run_id           text NOT NULL REFERENCES simulation_runs ON DELETE CASCADE,
    intervention_id  text NOT NULL,
    kind             text NOT NULL,
    hospital_id      text NOT NULL REFERENCES hospitals,
    tick             integer NOT NULL,
    sim_time         timestamptz NOT NULL,
    wall_time        timestamptz,
    detail           jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, intervention_id, kind)
);

-- The coordination feed as last shown on the dashboard (replaced per run on each change).
CREATE TABLE IF NOT EXISTS agent_activity (
    run_id       text NOT NULL REFERENCES simulation_runs ON DELETE CASCADE,
    seq          integer NOT NULL,
    event_id     text NOT NULL,
    tick         integer NOT NULL,
    sim_time     timestamptz NOT NULL,
    title        text NOT NULL,
    detail       text NOT NULL,
    kind         text NOT NULL,
    track        text NOT NULL,
    origin       text NOT NULL,
    severity     text NOT NULL,
    hospital_id  text,
    wall_time    timestamptz,
    PRIMARY KEY (run_id, seq)
);
