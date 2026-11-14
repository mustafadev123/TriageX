-- Prompt 6: grounded AI operational briefs (Gemini primary/fallback, or the template).
-- Only the rendered brief and which metric keys it cited are stored; numbers come from the API.
CREATE TABLE IF NOT EXISTS ai_briefs (
    brief_id         text PRIMARY KEY,
    run_id           text NOT NULL,
    run_version      integer NOT NULL,
    at_tick          integer NOT NULL,
    clock            text NOT NULL,
    provider         text NOT NULL CHECK (provider IN ('gemini', 'gemini_fallback', 'template')),
    model            text,
    latency_ms       double precision NOT NULL,
    fallback_reason  text,
    headline         text NOT NULL,
    bullets          jsonb NOT NULL,
    action           text NOT NULL,
    metric_keys      text[] NOT NULL,
    generated_at     timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_briefs_run_idx ON ai_briefs (run_id, generated_at DESC);
