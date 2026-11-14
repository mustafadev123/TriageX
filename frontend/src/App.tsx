import { useCallback, useEffect, useState } from 'react'
import { ActionPanel } from './components/ActionPanel'
import { CoordinationFeed } from './components/CoordinationFeed'
import { FuturesCharts } from './components/FuturesCharts'
import { HospitalDetail, HospitalMap } from './components/HospitalMap'
import { ImpactHero } from './components/ImpactHero'
import { TechnicalPanel } from './components/TechnicalPanel'
import { TimelineControls } from './components/TimelineControls'
import { TopBar } from './components/TopBar'
import { useDemo } from './hooks/useDemo'
import type { TrackName } from './types/dashboard'

export default function App() {
  const demo = useDemo()
  const { state } = demo
  const [track, setTrack] = useState<TrackName>('triagex')
  const [selected, setSelected] = useState<string | null>(null)
  const [technical, setTechnical] = useState(false)

  const fullscreen = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen()
    else void document.documentElement.requestFullscreen?.()
  }, [])

  // Presenter keys: → / PageDown = Next event (clickers send these), Space = play/pause.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      if (el && ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) && e.key !== 'PageDown') return
      if (e.key === 'ArrowRight' || e.key === 'PageDown') {
        e.preventDefault()
        demo.nextEvent()
      } else if (e.key === 'ArrowLeft' || e.key === 'PageUp') {
        e.preventDefault()
        demo.stepBack()
      } else if (e.key === ' ') {
        e.preventDefault()
        demo.togglePlay()
      } else if (e.key === 'f' || e.key === 'F') {
        fullscreen()
      } else if (e.key === 'd' || e.key === 'D') {
        setTechnical((t) => !t)
      } else if (e.key === 'R' && e.shiftKey) {
        void demo.reset()
      } else if (e.key === 'Escape') {
        setTechnical(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [demo, fullscreen])

  if (!state) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="panel max-w-md p-6 text-center">
          <div className="text-[22px] font-bold">TriageX</div>
          {demo.error ? (
            <>
              <p role="alert" className="mt-2 text-[14px] text-st-overloaded">
                Cannot reach the TriageX backend: {demo.error}
              </p>
              <button
                type="button"
                className="mt-4 rounded-lg bg-triagex px-4 py-2 font-semibold text-ink"
                onClick={() => window.location.reload()}
              >
                Retry
              </button>
            </>
          ) : (
            <p className="mt-2 text-[14px] text-muted">Loading simulation…</p>
          )}
        </div>
      </div>
    )
  }

  const focus = selected ?? state.curves.focus_hospital_id

  return (
    <div className="mx-auto flex h-full max-w-[2200px] flex-col gap-3 p-3">
      <TopBar
        state={state}
        scenarioId={demo.scenarioId}
        onScenario={(id) => {
          demo.setScenarioId(id)
          void demo.reset(id)
        }}
      />

      {demo.error && (
        <div
          role="alert"
          className="flex items-center justify-between rounded-lg border border-st-overloaded/50 bg-st-overloaded/10 px-3 py-2 text-[13px]"
        >
          <span>{demo.error}</span>
          <button type="button" onClick={demo.dismissError} className="text-muted hover:text-text">
            Dismiss
          </button>
        </div>
      )}

      <main
        className="grid min-h-0 flex-1 gap-3"
        style={{ gridTemplateColumns: 'clamp(270px, 22vw, 420px) minmax(0, 1fr) clamp(300px, 24vw, 440px)' }}
      >
        <div className="flex min-h-0 flex-col">
          <HospitalMap
            state={state}
            playhead={demo.playhead}
            track={track}
            onTrackChange={setTrack}
            selected={focus}
            onSelect={setSelected}
          />
          <HospitalDetail state={state} playhead={demo.playhead} hospitalId={focus} />
        </div>

        <div className="flex min-h-0 flex-col gap-3">
          <ImpactHero state={state} playhead={demo.playhead} />
          <FuturesCharts state={state} playhead={demo.playhead} />
        </div>

        <div className="flex min-h-0 flex-col">
          <ActionPanel
            state={state}
            playhead={demo.playhead}
            busy={demo.busy}
            attention={demo.attention}
            lastReplyMs={demo.lastReplyMs}
            onApprove={() => void demo.approve()}
            onSimulateReply={() => void demo.simulateReply()}
          />
          {demo.hint && (
            <p className="mt-2 rounded-lg bg-st-approaching/10 px-3 py-1.5 text-[12.5px] text-st-approaching" role="status">
              {demo.hint}
            </p>
          )}
          <CoordinationFeed state={state} playhead={demo.playhead} />
        </div>
      </main>

      <TimelineControls
        demo={demo}
        state={state}
        onToggleTechnical={() => setTechnical((t) => !t)}
        onFullscreen={fullscreen}
      />

      {technical && <TechnicalPanel state={state} onClose={() => setTechnical(false)} />}
    </div>
  )
}
