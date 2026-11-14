import type { DashboardState, StaffingRequest } from '../types/dashboard'

/** FaceTime is the primary channel; if no call arrives within this time, say so. */
export const FACETIME_WAIT_S = 60

/** Fallback order: FaceTime -> iMessage reply -> labeled simulated button. */
export function fallbackNotice(state: DashboardState, req: StaffingRequest, nowMs: number): string | null {
  if (req.confirmed_staff >= req.requested_staff || req.status === 'expired') return null
  const ft = state.facetime
  const call = ft?.calls[0]
  const imessage = (req.outreach ?? []).some((o) => o.status === 'sent' || o.status === 'confirmed')
  const options = imessage ? 'an iMessage reply “IN” or the simulated fallback' : 'the simulated fallback'
  if (call && call.status === 'ended' && call.confirmed_staff === 0) {
    return `FaceTime call ended without a confirmation. Use ${options}.`
  }
  const waited = (nowMs - Date.parse(req.approved_at)) / 1000
  if (ft?.enabled && !call && waited >= FACETIME_WAIT_S) {
    return `No FaceTime call within ${FACETIME_WAIT_S} s. Use ${options}.`
  }
  return null
}
