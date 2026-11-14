import { activeRequest, pointAt, recommendedProposal } from '../lib/demo'
import type { Attention, Busy } from '../hooks/useDemo'
import type { DashboardState } from '../types/dashboard'

interface Props {
  state: DashboardState
  playhead: number
  busy: Busy
  attention: Attention
  lastReplyMs: number | null
  onApprove: () => void
  onSimulateReply: () => void
}

/**
 * The coordination action. Approval and confirmations are always explicit presenter
 * actions. Confirmations arrive either from the simulated fallback (labeled) or, after
 * Phase 6, from a verified Chert webhook; both update this same panel via the API.
 */
export function ActionPanel({ state, playhead, busy, attention, lastReplyMs, onApprove, onSimulateReply }: Props) {
  const card = recommendedProposal(state)
  const req = activeRequest(state)
  const hospital = state.hospitals.find((h) => h.id === (req?.hospital_id ?? card?.hospital_id))

  if (!card || playhead < card.tick) {
    return (
      <section className="panel p-4" aria-label="Coordination">
        <div className="eyebrow">TriageX coordination</div>
        <div className="mt-2 flex items-center gap-2 text-[15px]">
          <span className="h-2 w-2 rounded-full bg-st-normal" /> Monitoring 5 emergency departments
        </div>
        <p className="mt-1 text-[13px] text-muted">Forecasting saturation 2 hours ahead, every 15 minutes.</p>
      </section>
    )
  }

  if (!req) {
    return (
      <section
        className={`panel border-st-approaching/60 p-4 ${attention === 'approve' ? 'attention' : ''}`}
        aria-label="Coordination"
      >
        <div className="eyebrow text-st-approaching">Outreach proposed · {card.clock}</div>
        <div className="mt-1 text-[20px] font-bold">{card.title}</div>
        <p className="mt-1 text-[13px] text-muted">
          2-hour forecast <span className="font-semibold text-st-overloaded">{Math.round(card.forecast_saturation_pct)}%</span>{' '}
          of treatment spaces (now {Math.round(card.current_saturation_pct)}%).
        </p>
        <button
          type="button"
          onClick={onApprove}
          disabled={busy !== null}
          className="mt-3 w-full rounded-xl bg-triagex px-4 py-3 text-[16px] font-bold text-ink shadow-[0_0_24px_rgb(45_212_191/0.35)] transition hover:bg-triagex-strong disabled:opacity-60"
        >
          {busy === 'approve' ? 'Approving…' : 'Approve outreach'}
        </button>
        <p className="mt-2 text-[11px] text-faint">Human approval required. Fictional demo staff only.</p>
      </section>
    )
  }

  const shiftActive = req.confirmed_staff > 0 && playhead >= req.effective_tick
  const full = req.confirmed_staff >= req.requested_staff
  const expired = req.status === 'expired'
  const capacityNow = pointAt(state, req.hospital_id, 'triagex', req.approved_tick)?.staffed_capacity
  const capacityOnShift = pointAt(state, req.hospital_id, 'triagex', req.effective_tick)?.staffed_capacity
  const statusLine = shiftActive
    ? `Shift active since ${req.effective_clock}`
    : expired
      ? 'Request expired'
      : full
        ? `All confirmed · shift starts ${req.effective_clock}`
        : `Awaiting confirmations · shift starts ${req.effective_clock}`

  return (
    <section
      className={`panel p-4 ${shiftActive ? 'glow-teal border-triagex/60' : 'border-triagex/30'}`}
      aria-label="Coordination"
    >
      <div className="eyebrow text-triagex-strong">Outreach approved · {req.approved_clock}</div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <div className="text-[18px] font-bold">{hospital?.short_name} surge staffing</div>
        <div className="tabular text-[26px] leading-none font-bold" data-testid="confirmed-count">
          {req.confirmed_staff}
          <span className="text-[16px] text-muted">/{req.requested_staff}</span>
        </div>
      </div>
      <div className="mt-1 text-[13px] text-muted" data-testid="request-status">
        {statusLine}
      </div>

      <ol className="mt-3 flex gap-2" aria-label="Staff confirmations">
        {Array.from({ length: req.requested_staff }, (_, i) => {
          const c = req.confirmations[i]
          return (
            <li
              key={i}
              className={`flex-1 rounded-lg border px-2 py-1.5 text-center text-[11px] ${
                c ? 'feed-in border-triagex/50 bg-triagex/10 text-triagex-strong' : 'border-dashed border-line text-faint'
              }`}
            >
              {c ? (
                <>
                  <div className="truncate font-semibold">{c.display_name.replace(/ \(fictional RN\)$/, '')}</div>
                  <div className="text-[10px] uppercase tracking-wider text-muted">
                    {c.source === 'webhook' ? 'Live reply' : 'Simulated'}
                  </div>
                </>
              ) : (
                'Awaiting'
              )}
            </li>
          )
        })}
      </ol>

      {req.confirmed_staff > 0 && capacityNow !== undefined && capacityOnShift !== undefined && (
        <p className="mt-2 text-[13px]">
          {hospital?.short_name} staffed capacity{' '}
          <span className="tabular font-semibold">{capacityNow}</span> →{' '}
          <span className="tabular font-semibold text-triagex-strong">{capacityOnShift}</span>{' '}
          <span className="text-muted">{shiftActive ? 'now' : `from ${req.effective_clock}`}</span>
        </p>
      )}

      {!full && !expired && (
        <button
          type="button"
          onClick={onSimulateReply}
          disabled={busy !== null}
          className={`mt-3 w-full rounded-xl border border-triagex/50 bg-triagex/10 px-4 py-2.5 text-[15px] font-semibold text-triagex-strong transition hover:bg-triagex/20 disabled:opacity-60 ${
            attention === 'reply' ? 'attention' : ''
          }`}
        >
          {busy === 'reply' ? 'Recording reply…' : 'Simulate staff reply “IN”'}
          <span className="ml-2 rounded bg-ink/60 px-1.5 py-0.5 text-[10px] font-semibold tracking-wider text-muted">
            SIMULATED FALLBACK
          </span>
        </button>
      )}
      {lastReplyMs !== null && (
        <p className="mt-1.5 text-[11px] text-faint" data-testid="reply-latency">
          Last confirmation recalculated and returned in {lastReplyMs} ms
        </p>
      )}
    </section>
  )
}
