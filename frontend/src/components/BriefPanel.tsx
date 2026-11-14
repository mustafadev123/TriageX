import { useState } from 'react'
import { api } from '../services/api'
import type { DashboardState, OperationalBrief } from '../types/dashboard'

const BADGE: Record<OperationalBrief['provider'], { label: string; cls: string }> = {
  gemini: { label: 'Gemini', cls: 'bg-triagex/15 text-triagex-strong' },
  gemini_fallback: { label: 'Gemini fallback', cls: 'bg-st-approaching/15 text-st-approaching' },
  template: { label: 'Template · AI unavailable', cls: 'bg-ink/60 text-muted' },
}

/**
 * Grounded operational brief, generated on request for the current playhead. The backend uses
 * only what is visible at the playhead and fills in every number itself; the badge says which
 * provider wrote the text, and a fallback is always shown with its reason.
 */
export function BriefPanel({ state, playhead }: { state: DashboardState; playhead: number }) {
  const [stored, setBrief] = useState<OperationalBrief | null>(null)
  const [busy, setBusy] = useState(false)
  const [hidden, setHidden] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const runId = state.run.run_id
  // A reset starts a new run: a brief from an earlier run is never shown.
  const brief = stored && stored.run_id === runId ? stored : null

  const generate = async () => {
    setBusy(true)
    setError(null)
    setHidden(false)
    try {
      setBrief(await api.brief(runId, playhead))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Brief failed')
    } finally {
      setBusy(false)
    }
  }

  const clock = state.timeline.clocks[playhead] ?? ''
  return (
    <section className="panel mt-3 shrink-0 px-3 py-2" aria-label="Operational brief" data-testid="brief-panel">
      <div className="flex items-center justify-between gap-2">
        <div className="eyebrow">AI operational brief</div>
        <button
          type="button"
          onClick={() => void generate()}
          disabled={busy}
          className="rounded-lg border border-line px-2 py-1 text-[12px] font-semibold text-muted transition hover:text-text disabled:opacity-60"
        >
          {busy ? 'Writing…' : `Generate (${clock})`}
        </button>
      </div>
      {error && <p className="mt-1 text-[12px] text-st-overloaded">{error}</p>}
      {brief && !hidden && (
        // Floats above the charts so the coordination feed keeps its space.
        <div
          className="fixed right-4 bottom-[104px] z-20 rounded-xl border border-line bg-panel-2 max-h-[60vh] w-[min(440px,calc(100vw-32px))] overflow-y-auto p-3 text-[12.5px] shadow-2xl"
          data-testid="brief"
          role="dialog"
          aria-label="AI operational brief"
        >
          <div className="flex items-center gap-2">
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wider uppercase ${BADGE[brief.provider].cls}`}>
              {BADGE[brief.provider].label}
            </span>
            <span className="tabular text-[11px] text-faint">
              at {brief.clock}
              {brief.model ? ` · ${brief.model}` : ''} · {Math.round(brief.latency_ms)} ms
            </span>
            <button
              type="button"
              onClick={() => setHidden(true)}
              className="ml-auto rounded px-1.5 text-[14px] text-muted hover:text-text"
              aria-label="Close brief"
            >
              ×
            </button>
          </div>
          <p className="mt-1.5 text-[14px] font-semibold">{brief.headline}</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-muted">
            {brief.bullets.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
          <p className="mt-1.5">→ {brief.action}</p>
          {brief.fallback_reason && (
            <p className="mt-1 text-[11px] text-st-approaching" data-testid="brief-fallback">
              Fallback: {brief.fallback_reason}
            </p>
          )}
          <p className="mt-1 text-[10.5px] text-faint">{brief.notice}</p>
        </div>
      )}
    </section>
  )
}
