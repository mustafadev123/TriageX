import { memo } from 'react'
import { visibleFeed } from '../lib/demo'
import type { DashboardState, FeedEvent } from '../types/dashboard'

const SEVERITY: Record<FeedEvent['severity'], string> = {
  info: '#64748b',
  warning: '#fbbf24',
  critical: '#f43f5e',
  success: '#2dd4bf',
}

const ORIGIN: Record<FeedEvent['origin'], { label: string; cls: string }> = {
  simulation: { label: 'Model', cls: 'text-faint' },
  operator: { label: 'Operator', cls: 'text-shared' },
  simulated_reply: { label: 'Simulated reply', cls: 'text-st-approaching' },
  webhook: { label: 'Live reply', cls: 'text-triagex-strong' },
}

export const CoordinationFeed = memo(function CoordinationFeed({ state, playhead }: { state: DashboardState; playhead: number }) {
  const events = visibleFeed(state, playhead)
  return (
    <section className="panel mt-3 flex min-h-0 flex-1 flex-col p-3" aria-label="Live coordination feed">
      <div className="mb-2 flex items-center justify-between">
        <div className="eyebrow">Live coordination feed</div>
        <div className="tabular text-[12px] text-faint">{events.length} events</div>
      </div>
      <ol className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1" data-testid="feed">
        {events.length === 0 && (
          <li className="px-1 py-2 text-[13px] text-faint">Waiting for the first operational event…</li>
        )}
        {events.map((e) => (
          <li
            key={e.id}
            className="feed-in relative rounded-lg bg-ink/50 py-2 pr-2 pl-3"
            style={{ boxShadow: `inset 3px 0 0 ${SEVERITY[e.severity]}` }}
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[14px] font-semibold">{e.title}</span>
              <span className="tabular shrink-0 text-[12px] text-muted">{e.clock}</span>
            </div>
            <div className="text-[12.5px] leading-snug text-muted">{e.detail}</div>
            <div className="mt-0.5 flex gap-2 text-[10px] font-semibold tracking-wider uppercase">
              <span className={ORIGIN[e.origin].cls}>{ORIGIN[e.origin].label}</span>
              {e.track === 'baseline' && <span className="text-baseline">Without TriageX</span>}
              {e.track === 'both' && e.kind.startsWith('diversion') && <span className="text-faint">Both futures</span>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
})
