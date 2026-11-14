// A run.updated that arrives while the presenter's own request is in flight (for example a
// live FaceTime confirmation during a click) must be fetched once the request returns.
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import approved from '../test/fixtures/approved.json'
import confirmed1 from '../test/fixtures/confirmed1.json'
import type { DashboardState } from '../types/dashboard'

vi.mock('../services/api', () => ({
  api: {
    activeState: vi.fn(),
    run: vi.fn(),
    simulateReply: vi.fn(),
    approve: vi.fn(),
    reset: vi.fn(),
  },
}))

const { api } = await import('../services/api')
const { useDemo } = await import('./useDemo')

const RUN = 'run-under-test'
const withVersion = (fixture: unknown, version: number): DashboardState => {
  const s = structuredClone(fixture) as DashboardState
  s.run.run_id = RUN
  s.run.version = version
  return s
}

class FakeSocket {
  static last: FakeSocket | null = null
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  constructor() {
    FakeSocket.last = this
  }
  close() {}
  emit(msg: object) {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }
}

describe('useDemo live sync', () => {
  const original = globalThis.WebSocket
  beforeEach(() => {
    globalThis.WebSocket = FakeSocket as unknown as typeof WebSocket
    vi.mocked(api.activeState).mockResolvedValue(withVersion(approved, 2))
  })
  afterEach(() => {
    globalThis.WebSocket = original
    vi.clearAllMocks()
  })

  it('fetches a run.updated that arrived during an in-flight simulated reply', async () => {
    const { result } = renderHook(() => useDemo())
    await waitFor(() => expect(result.current.state?.run.version).toBe(2))

    let respond!: (s: DashboardState) => void
    vi.mocked(api.simulateReply).mockReturnValue(new Promise((r) => (respond = r)))
    const live = withVersion(confirmed1, 4) // our reply (v3) plus a live confirmation (v4)
    vi.mocked(api.run).mockResolvedValue(live)

    let pending!: Promise<void>
    act(() => {
      pending = result.current.simulateReply()
    })
    act(() => FakeSocket.last!.emit({ type: 'run.updated', run_id: RUN, version: 3 })) // our echo
    act(() => FakeSocket.last!.emit({ type: 'run.updated', run_id: RUN, version: 4 })) // live
    expect(api.run).not.toHaveBeenCalled() // nothing fetched while our request is in flight

    await act(async () => {
      respond(withVersion(confirmed1, 3))
      await pending
    })
    await waitFor(() => expect(result.current.state?.run.version).toBe(4))
    expect(api.run).toHaveBeenCalledTimes(1)
  })

  it('does not refetch when the response already includes every announced version', async () => {
    const { result } = renderHook(() => useDemo())
    await waitFor(() => expect(result.current.state?.run.version).toBe(2))

    let respond!: (s: DashboardState) => void
    vi.mocked(api.simulateReply).mockReturnValue(new Promise((r) => (respond = r)))
    let pending!: Promise<void>
    act(() => {
      pending = result.current.simulateReply()
    })
    act(() => FakeSocket.last!.emit({ type: 'run.updated', run_id: RUN, version: 3 }))
    await act(async () => {
      respond(withVersion(confirmed1, 3))
      await pending
    })
    expect(result.current.state?.run.version).toBe(3)
    expect(api.run).not.toHaveBeenCalled()
  })
})
