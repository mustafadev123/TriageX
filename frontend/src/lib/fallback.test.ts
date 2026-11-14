// Fallback order: FaceTime -> iMessage -> labeled simulated button.
import { describe, expect, it } from 'vitest'
import approved from '../test/fixtures/approved.json'
import type { DashboardState, FaceTimeView } from '../types/dashboard'
import { FACETIME_WAIT_S, fallbackNotice } from './fallback'

const state = (facetime?: FaceTimeView, outreach: string[] = []): DashboardState => {
  const s = structuredClone(approved) as unknown as DashboardState
  s.facetime = facetime
  s.staffing_requests[0].approved_at = '2026-11-14T17:00:00Z'
  s.staffing_requests[0].outreach = outreach.map((status, i) => ({
    outreach_id: `out-${i}`, recipient: `•••010${i}`, status: status as 'sent', sent_at: null,
    confirmed_at: null, error: null, send_ms: null,
  }))
  return s
}
const approvedAt = (s: DashboardState) => Date.parse(s.staffing_requests[0].approved_at)
const call = (status: 'connected' | 'ended', confirmed = 0) => ({
  call_id: 'abc', status, connected_at: '', ended_at: null, checks: [], confirmed_staff: confirmed,
})

describe('fallbackNotice', () => {
  it('says nothing before the 60 s wait or without the FaceTime agent', () => {
    const s = state({ enabled: true, detail: '', calls: [] })
    expect(fallbackNotice(s, s.staffing_requests[0], approvedAt(s) + 59_000)).toBeNull()
    const off = state({ enabled: false, detail: '', calls: [] })
    expect(fallbackNotice(off, off.staffing_requests[0], approvedAt(off) + 600_000)).toBeNull()
  })

  it('offers iMessage and the simulated fallback when no call arrived in 60 s', () => {
    const s = state({ enabled: true, detail: '', calls: [] }, ['sent'])
    expect(fallbackNotice(s, s.staffing_requests[0], approvedAt(s) + FACETIME_WAIT_S * 1000)).toBe(
      'No FaceTime call within 60 s. Use an iMessage reply “IN” or the simulated fallback.',
    )
    const noImessage = state({ enabled: true, detail: '', calls: [] })
    expect(fallbackNotice(noImessage, noImessage.staffing_requests[0], approvedAt(noImessage) + 61_000)).toBe(
      'No FaceTime call within 60 s. Use the simulated fallback.',
    )
  })

  it('reports a call that ended without a confirmation, but not one that confirmed', () => {
    const ended = state({ enabled: true, detail: '', calls: [call('ended')] })
    expect(fallbackNotice(ended, ended.staffing_requests[0], approvedAt(ended))).toMatch(/^FaceTime call ended without a confirmation/)
    const ok = state({ enabled: true, detail: '', calls: [call('ended', 1)] })
    expect(fallbackNotice(ok, ok.staffing_requests[0], approvedAt(ok) + 120_000)).toBeNull()
    const live = state({ enabled: true, detail: '', calls: [call('connected')] })
    expect(fallbackNotice(live, live.staffing_requests[0], approvedAt(live) + 120_000)).toBeNull()
  })
})
