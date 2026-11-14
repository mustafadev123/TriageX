import { useEffect, useState } from 'react'
import { api } from '../services/api'
import { APP_NAME, TAGLINE } from '../types/branding'
import type { DashboardState, ScenarioInfo } from '../types/dashboard'

interface Props {
  state: DashboardState
  scenarioId: string
  onScenario: (id: string) => void
}

export function TopBar({ state, scenarioId, onScenario }: Props) {
  const [scenarios, setScenarios] = useState<ScenarioInfo[]>([])
  useEffect(() => {
    api.scenarios().then(setScenarios).catch(() => setScenarios([]))
  }, [])

  return (
    <header className="flex items-center justify-between gap-4 px-1">
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-triagex/15 text-[18px] font-black text-triagex-strong">
          T
        </div>
        <div className="min-w-0">
          <h1 className="text-[20px] leading-tight font-bold tracking-tight">{APP_NAME}</h1>
          <p className="truncate text-[12.5px] text-muted">{TAGLINE}</p>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <label className="flex items-center gap-2 text-[12px] text-muted">
          Scenario
          <select
            value={scenarioId}
            onChange={(e) => onScenario(e.target.value)}
            className="rounded-lg border border-line bg-panel px-2 py-1.5 text-[13px] font-semibold text-text"
            aria-label="Scenario"
          >
            {(scenarios.length ? scenarios : [{ id: state.run.scenario.id, name: state.run.scenario.name }]).map(
              (s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ),
            )}
          </select>
        </label>
        <span className="tabular text-[12px] text-faint">seed {state.run.seed}</span>
        <span
          role="note"
          className="rounded-full border border-st-approaching/40 bg-st-approaching/10 px-3 py-1 text-[11.5px] font-medium text-st-approaching"
        >
          {state.run.notice}
        </span>
      </div>
    </header>
  )
}
