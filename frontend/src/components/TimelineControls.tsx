import { SPEEDS, type DemoController } from '../hooks/useDemo'
import { clockAt, nextStepIndex, presentationSteps, stepSatisfied, windowRange } from '../lib/demo'
import type { DashboardState } from '../types/dashboard'

interface Props {
  demo: DemoController
  state: DashboardState
  onToggleTechnical: () => void
  onFullscreen: () => void
}

export function TimelineControls({ demo, state, onToggleTechnical, onFullscreen }: Props) {
  const { start, last } = windowRange(state)
  const steps = presentationSteps(state)
  const nextIndex = nextStepIndex(state, demo.playhead)
  const next = demo.next
  const nextLabel =
    next?.kind === 'done' ? 'Demo complete' : next ? next.step.label : 'Next event'
  const markerTicks = steps.map((s) => s.tick)

  return (
    <footer className="panel flex items-center gap-3 px-3 py-2" aria-label="Presentation controls">
      <button
        type="button"
        onClick={() => void demo.reset()}
        disabled={demo.busy === 'reset'}
        className="rounded-lg border border-line px-3 py-2 text-[13px] font-medium text-muted hover:text-text"
        title="Reset demo (Shift+R)"
      >
        {demo.busy === 'reset' ? 'Resetting…' : 'Reset'}
      </button>
      <button
        type="button"
        onClick={demo.togglePlay}
        className="w-[76px] rounded-lg border border-line px-3 py-2 text-[13px] font-semibold"
        aria-label={demo.playing ? 'Pause' : 'Play'}
        title="Play / pause (Space)"
      >
        {demo.playing ? '❚❚ Pause' : '▶ Play'}
      </button>

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-3">
          <span className="tabular w-[52px] text-[22px] font-bold" data-testid="clock">
            {clockAt(state, demo.playhead)}
          </span>
          <div className="relative flex-1">
            <input
              type="range"
              className="scrubber w-full"
              min={start}
              max={last}
              step={1}
              value={demo.playhead}
              onChange={(e) => demo.scrub(Number(e.target.value))}
              aria-label="Simulation time"
              aria-valuetext={clockAt(state, demo.playhead)}
            />
            <div className="pointer-events-none absolute inset-x-0 -bottom-1 h-1">
              {markerTicks.map((t, i) => (
                <span
                  key={`${t}-${i}`}
                  className="absolute h-1 w-1 rounded-full bg-triagex/70"
                  style={{ left: `calc(${((t - start) / (last - start)) * 100}% - 2px)` }}
                />
              ))}
            </div>
          </div>
          <span className="tabular text-[12px] text-faint">
            {state.timeline.demo_window.start_clock}–{state.timeline.demo_window.end_clock}
          </span>
        </div>
        <ol className="mt-1 flex gap-1.5 text-[11px]" aria-label="Presentation steps">
          {steps.map((s, i) => {
            const done = stepSatisfied(state, s, demo.playhead) && (nextIndex === null || i < nextIndex)
            const current = i === nextIndex
            return (
              <li
                key={s.id}
                className={`rounded-full px-2 py-0.5 ${
                  current
                    ? 'bg-triagex/15 font-semibold text-triagex-strong'
                    : done
                      ? 'text-muted'
                      : 'text-faint'
                }`}
              >
                {done ? '✓ ' : `${i + 1}. `}
                {s.label}
              </li>
            )
          })}
        </ol>
      </div>

      <label className="flex items-center gap-1 text-[12px] text-muted">
        Speed
        <select
          value={demo.speed}
          onChange={(e) => demo.setSpeed(Number(e.target.value))}
          className="rounded-md border border-line bg-panel px-1.5 py-1 text-text"
          aria-label="Playback speed"
        >
          {SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
      </label>

      <button
        type="button"
        onClick={onToggleTechnical}
        className="rounded-lg border border-line px-2.5 py-2 text-[12px] text-muted hover:text-text"
        title="Technical details (D)"
      >
        Technical
      </button>
      <button
        type="button"
        onClick={onFullscreen}
        className="rounded-lg border border-line px-2.5 py-2 text-[12px] text-muted hover:text-text"
        title="Fullscreen (F)"
      >
        ⛶
      </button>
      <button
        type="button"
        onClick={demo.nextEvent}
        disabled={next?.kind === 'done'}
        className="min-w-[210px] rounded-xl bg-triagex px-4 py-2.5 text-left text-ink shadow-[0_0_24px_rgb(45_212_191/0.3)] transition hover:bg-triagex-strong disabled:bg-panel-2 disabled:text-muted disabled:shadow-none"
        data-testid="next-event"
        title="Next event (→ or Page Down)"
      >
        <div className="text-[10px] font-bold tracking-widest uppercase opacity-70">Next event</div>
        <div className="text-[15px] font-bold">{nextLabel} →</div>
      </button>
    </footer>
  )
}
