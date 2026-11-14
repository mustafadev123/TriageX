import { describe, expect, it } from 'vitest'
import approved from '../test/fixtures/approved.json'
import confirmed1 from '../test/fixtures/confirmed1.json'
import confirmed3 from '../test/fixtures/confirmed3.json'
import initial from '../test/fixtures/initial.json'
import type { DashboardState } from '../types/dashboard'
import {
  axisMaxima,
  chartRows,
  mapStatus,
  nextAction,
  presentationSteps,
  revealedDivergence,
  savedSoFar,
  visibleFeed,
} from './demo'

// Fixtures are real API responses exported by scripts/export_fixtures.py.
const S0 = initial as unknown as DashboardState
const SA = approved as unknown as DashboardState
const S1 = confirmed1 as unknown as DashboardState
const S3 = confirmed3 as unknown as DashboardState

const T = { onset: 16, proposal: 17, approval: 18, shift: 21, end: 35 } // 16:00 .. 20:45

describe('fixtures reflect the verified I-85 timeline', () => {
  it('has proposal 16:15, approval 16:30, shift active 17:15', () => {
    const req = S3.staffing_requests[0]
    expect([req.proposal_clock, req.approved_clock, req.effective_clock]).toEqual([
      '16:15',
      '16:30',
      '17:15',
    ])
    expect(S3.divergence).toEqual({ tick: T.shift, clock: '17:15' })
    expect(S0.divergence).toBeNull()
  })
})

describe('no premature divergence', () => {
  it('shows a single shared line everywhere when nothing was confirmed', () => {
    for (const state of [S0, SA]) {
      const rows = chartRows(state, T.end)
      expect(rows.every((r) => r.baselineSat === null && r.triagexSat === null)).toBe(true)
      expect(rows.every((r) => r.sharedSat !== null && r.sharedHours !== null)).toBe(true)
      expect(savedSoFar(state, T.end)).toBe(0)
    }
  })

  it('keeps the futures identical before 17:15 even though Track B is already recomputed', () => {
    for (let playhead = 12; playhead < T.shift; playhead++) {
      const rows = chartRows(S3, playhead)
      expect(rows.some((r) => r.baselineSat !== null || r.triagexSat !== null)).toBe(false)
      expect(rows.some((r) => r.savedBand !== null)).toBe(false)
      expect(savedSoFar(S3, playhead)).toBe(0)
      expect(revealedDivergence(S3, playhead)).toBeNull()
    }
  })

  it('never reveals data beyond the playhead', () => {
    const rows = chartRows(S3, 24)
    for (const r of rows.filter((x) => x.tick > 24)) {
      expect([r.sharedSat, r.baselineSat, r.triagexSat, r.sharedHours, r.triagexHours]).toEqual([
        null, null, null, null, null,
      ])
    }
  })
})

describe('divergence after 17:15', () => {
  it('splits the two futures from the API divergence tick using API values', () => {
    const rows = chartRows(S3, T.end)
    const byTick = new Map(rows.map((r) => [r.tick, r]))
    expect(byTick.get(T.shift)?.sharedSat).toBeNull()
    for (const p of S3.curves.points.filter((x) => x.tick >= T.shift)) {
      const r = byTick.get(p.tick)!
      expect(r.baselineSat).toBe(p.baseline_focus_saturation_pct)
      expect(r.triagexSat).toBe(p.triagex_focus_saturation_pct)
      expect(r.baselineHours).toBe(p.baseline_ambulance_hours_cumulative)
      expect(r.triagexHours).toBe(p.triagex_ambulance_hours_cumulative)
    }
    expect(rows.some((r) => r.baselineHours !== r.triagexHours && r.baselineHours !== null)).toBe(
      true,
    )
  })

  it('counts ambulance-hours saved from the API, reaching the final impact at the end', () => {
    expect(savedSoFar(S3, T.shift - 1)).toBe(0)
    expect(savedSoFar(S3, T.end)).toBe(-S3.impact.primary.delta)
    expect(savedSoFar(S3, T.end)).toBe(9.25)
    expect(savedSoFar(S1, T.end)).toBe(-S1.impact.primary.delta)
  })

  it('reveals the forecast only from data known at the playhead', () => {
    const rows = chartRows(S3, 20)
    const grady = S3.series.find((s) => s.hospital_id === 'grady')!.triagex
    for (const r of rows.filter((x) => x.forecastSat !== null)) {
      expect(r.tick - 8).toBeLessThanOrEqual(20)
      expect(r.forecastSat).toBe(grady[r.tick - 8].forecast_saturation_pct)
    }
    const forecastTicks = rows.filter((r) => r.forecastSat !== null).map((r) => r.tick)
    expect(Math.min(...forecastTicks)).toBe(20) // starts at the cursor
    expect(Math.max(...forecastTicks)).toBe(28) // ends 2 hours ahead
  })

  it('uses a fixed axis that covers every observed value', () => {
    const max = axisMaxima(S3)
    const observed = S3.curves.points.flatMap((p) => [
      p.baseline_focus_saturation_pct,
      p.triagex_focus_saturation_pct,
    ])
    expect(max.saturation).toBeGreaterThan(Math.max(...observed))
    expect(max.hours).toBeGreaterThanOrEqual(S3.impact.primary.baseline)
  })
})

describe('feed and map', () => {
  it('shows only events that have happened by the playhead', () => {
    expect(visibleFeed(S3, 15).length).toBe(0)
    const at1630 = visibleFeed(S3, T.approval).map((e) => e.title)
    expect(at1630).toContain('Outreach Approved')
    expect(at1630).toContain('Staff Confirmed')
    expect(at1630).not.toContain('Shift Active')
    expect(visibleFeed(S3, T.shift).map((e) => e.title)).toContain('Shift Active')
  })

  it('derives map status from API points', () => {
    expect(mapStatus(S0, 'grady', 'triagex', 12)).toBe('normal')
    expect(['overloaded', 'diverting']).toContain(mapStatus(S0, 'grady', 'triagex', 20))
  })
})

describe('presentation steps', () => {
  it('lists the six presenter steps on real API ticks', () => {
    expect(presentationSteps(S3).map((s) => [s.label, s.tick])).toEqual([
      ['Start Surge', 16],
      ['Show Prediction', 17],
      ['Approve Outreach', 18],
      ['Receive Confirmations', 18],
      ['Activate Shift', 21],
      ['Reveal Impact', 35],
    ])
  })

  it('advances the playhead but never approves or confirms on its own', () => {
    expect(nextAction(S0, 12)).toMatchObject({ kind: 'advance', toTick: 16 })
    expect(nextAction(S0, 16)).toMatchObject({ kind: 'advance', toTick: 17 })
    expect(nextAction(S0, 17)).toMatchObject({ kind: 'advance', toTick: 18 })
    expect(nextAction(S0, 18)).toMatchObject({ kind: 'await', step: { id: 'approve' } })
    expect(nextAction(SA, 18)).toMatchObject({ kind: 'await', step: { id: 'confirm' } })
    expect(nextAction(S1, 18)).toMatchObject({ kind: 'advance', toTick: 21 })
    expect(nextAction(S3, 21)).toMatchObject({ kind: 'advance', toTick: 35 })
    expect(nextAction(S3, 35)).toEqual({ kind: 'done' })
  })
})
