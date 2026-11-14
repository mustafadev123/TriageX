import { useEffect, useState } from 'react'
import { api } from '../services/api'
import type { DashboardState, IntegrationHealth, IntegrationsResponse } from '../types/dashboard'

type Json = Record<string, unknown>

interface Loaded {
  integrations?: IntegrationsResponse
  db?: Json
  insights?: Json
  diagnostics?: Json
  provenance?: Json
  errors: Record<string, string>
}

const STATUS_STYLE: Record<IntegrationHealth['status'], string> = {
  ok: 'text-st-normal',
  starting: 'text-st-approaching',
  not_configured: 'text-muted',
  not_implemented: 'text-faint',
  error: 'text-st-overloaded',
}

/** Engineering detail for judge Q&A. Every value is shown exactly as the API reports it. */
export function TechnicalPanel({ state, onClose }: { state: DashboardState; onClose: () => void }) {
  const [data, setData] = useState<Loaded>({ errors: {} })
  const runId = state.run.run_id
  const version = state.run.version

  useEffect(() => {
    let cancelled = false
    const errors: Record<string, string> = {}
    const grab = async <T,>(key: string, fn: () => Promise<T>) => {
      try {
        return await fn()
      } catch (e) {
        errors[key] = e instanceof Error ? e.message : String(e)
        return undefined
      }
    }
    void (async () => {
      const [integrations, db, diagnostics, provenance] = await Promise.all([
        grab('integrations', api.integrations),
        grab('db', api.dbStatus),
        grab('diagnostics', () => api.diagnostics(runId)),
        grab('provenance', api.provenance),
      ])
      const insights = db?.configured ? await grab('insights', () => api.dbInsights(runId)) : undefined
      if (!cancelled) setData({ integrations, db, diagnostics, provenance, insights, errors })
    })()
    return () => {
      cancelled = true
    }
  }, [runId, version])

  const writer = (data.db?.writer ?? {}) as Json
  const queries = (data.insights?.queries ?? {}) as Record<string, { columns: string[]; rows: unknown[][] }>
  const forecast = (data.diagnostics?.forecast_accuracy ?? {}) as Json
  const checkpoints = (data.provenance?.entire_checkpoints ?? []) as string[]

  return (
    <aside
      className="fixed inset-y-0 right-0 z-30 flex w-[min(640px,92vw)] flex-col border-l border-line bg-panel/98 shadow-2xl backdrop-blur"
      aria-label="Technical details"
    >
      <header className="flex items-center justify-between border-b border-line px-5 py-3">
        <div>
          <div className="eyebrow">Technical details</div>
          <div className="text-[13px] text-muted">
            Run <span className="font-mono">{runId}</span> · version {version}
          </div>
        </div>
        <button type="button" onClick={onClose} className="rounded-lg border border-line px-3 py-1.5 text-[13px]">
          Close
        </button>
      </header>
      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-4 text-[13px]">
        <Section title="Integrations (live from /api/integrations/health)">
          {data.integrations ? (
            <ul className="space-y-1.5">
              {(['simulation', 'database', 'ai_primary', 'ai_fallback', 'messaging', 'facetime'] as const).map((k) => {
                const h = data.integrations![k]
                if (!h) return null
                return (
                  <li key={k} className="flex gap-3">
                    <span className="w-28 shrink-0 font-semibold">{h.name}</span>
                    <span className={`w-28 shrink-0 font-mono ${STATUS_STYLE[h.status]}`}>{h.status}</span>
                    <span className="text-muted">{h.detail}</span>
                  </li>
                )
              })}
            </ul>
          ) : (
            <Missing error={data.errors.integrations} />
          )}
        </Section>

        <Section title="Tiger Data persistence (/api/db/status)">
          {data.db ? (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
              <KV k="Configured" v={String(data.db.configured)} />
              <KV k="Connected" v={String(data.db.connected)} />
              <KV k="PostgreSQL" v={String(data.db.server_version ?? '—')} />
              <KV k="TimescaleDB" v={String(data.db.timescaledb_version ?? '—')} />
              <KV k="Hypertable" v={String(data.db.hypertable)} />
              <KV k="Round trip" v={data.db.round_trip_ms ? `${data.db.round_trip_ms} ms` : '—'} />
              <KV k="Snapshots written" v={String(writer.snapshots_written ?? 0)} />
              <KV k="Rows written" v={String(writer.rows_written ?? 0)} />
              <KV k="Pending" v={String(writer.pending ?? 0)} />
              <KV k="Last error" v={String(writer.last_error ?? data.db.error ?? 'none')} />
            </dl>
          ) : (
            <Missing error={data.errors.db} />
          )}
        </Section>

        {data.insights && (
          <Section title={`Stored rows for this run (SQL over Tiger Data, ${String(data.insights.time_bucket_function)})`}>
            <QueryTable q={queries.stored_row_counts} />
            <div className="mt-3 text-[12px] text-muted">Hourly focus-hospital comparison</div>
            <QueryTable q={queries.hourly_focus_comparison} />
          </Section>
        )}

        <Section title="Development provenance (git + Entire)">
          {data.provenance ? (
            <dl className="grid grid-cols-1 gap-y-1">
              <KV k="Commit" v={`${String(data.provenance.commit ?? '—').slice(0, 12)} · ${String(data.provenance.commit_subject ?? '')}`} />
              <KV k="Entire checkpoints" v={`${checkpoints.length} in refs/entire/checkpoints`} />
              {checkpoints.length > 0 && <KV k="Latest IDs" v={checkpoints.slice(-3).join(', ')} />}
            </dl>
          ) : (
            <Missing error={data.errors.provenance} />
          )}
        </Section>

        <Section title="Model diagnostics">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
            <KV k="2-h forecast bias" v={forecast.bias_pct_points !== undefined ? `${forecast.bias_pct_points} pp` : '—'} />
            <KV k="2-h forecast MAE" v={forecast.mean_absolute_error_pct_points !== undefined ? `${forecast.mean_absolute_error_pct_points} pp` : '—'} />
            <KV k="Recompute history" v={((data.diagnostics?.recompute_history_ms ?? []) as number[]).map((m) => `${m} ms`).join(', ') || '—'} />
            <KV k="Engine version" v={String(data.provenance?.engine_version ?? '—')} />
          </dl>
          <ul className="mt-2 list-disc space-y-0.5 pl-5 text-muted">
            {((data.diagnostics?.model_limitations ?? []) as string[]).map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </Section>

        <Section title="Full-horizon comparison (spoilers — analysis only)">
          <ul className="space-y-1">
            {[state.impact.primary, ...state.impact.secondary].map((m) => (
              <li key={m.key} className="flex justify-between gap-3">
                <span className="text-muted">{m.label}</span>
                <span className="tabular">
                  {m.baseline} → {m.triagex} ({m.headline})
                </span>
              </li>
            ))}
            <li className="flex justify-between gap-3">
              <span className="text-muted">Recovery</span>
              <span>{state.impact.recovery.headline}</span>
            </li>
          </ul>
        </Section>
      </div>
    </aside>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-[12px] font-semibold tracking-wider text-muted uppercase">{title}</h3>
      {children}
    </section>
  )
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex gap-2">
      <dt className="shrink-0 text-faint">{k}</dt>
      <dd className="min-w-0 truncate font-mono text-[12px]">{v}</dd>
    </div>
  )
}

function Missing({ error }: { error?: string }) {
  return <p className="text-st-overloaded">Unavailable{error ? `: ${error}` : ''}</p>
}

function QueryTable({ q }: { q?: { columns: string[]; rows: unknown[][] } }) {
  if (!q) return null
  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-left text-[11.5px]">
        <thead className="bg-ink/60 text-faint">
          <tr>
            {q.columns.map((c) => (
              <th key={c} className="px-2 py-1 font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono">
          {q.rows.map((r, i) => (
            <tr key={i} className="border-t border-line-soft">
              {r.map((v, j) => (
                <td key={j} className="px-2 py-0.5 whitespace-nowrap">
                  {String(v ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
