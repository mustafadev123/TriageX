import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  activeRequest,
  clampTick,
  nextAction,
  recommendedProposal,
  windowRange,
  type NextAction,
} from '../lib/demo'
import { api } from '../services/api'
import type { DashboardState } from '../types/dashboard'

export const DEFAULT_SCENARIO = 'i85_pileup'
export const DEFAULT_SEED = 42
export const SPEEDS = [0.5, 1, 2] as const

export type Busy = null | 'loading' | 'reset' | 'approve' | 'reply'
export type Attention = null | 'approve' | 'reply'

export interface DemoController {
  state: DashboardState | null
  playhead: number
  playing: boolean
  speed: number
  busy: Busy
  error: string | null
  hint: string | null
  attention: Attention
  lastReplyMs: number | null
  next: NextAction | null
  scenarioId: string
  setScenarioId: (id: string) => void
  reset: (scenarioId?: string) => Promise<void>
  approve: () => Promise<void>
  simulateReply: () => Promise<void>
  nextEvent: () => void
  togglePlay: () => void
  scrub: (tick: number) => void
  stepBack: () => void
  setSpeed: (speed: number) => void
  dismissError: () => void
}

/**
 * Owns the demo: API state, playhead, playback, presenter actions, and live sync.
 *
 * Live sync: the backend broadcasts `run.updated` on /ws after any change (including a
 * future Chert webhook confirmation). The dashboard refetches the run, so a real reply
 * updates exactly the same UI as the simulated fallback.
 */
export function useDemo(): DemoController {
  const [state, setState] = useState<DashboardState | null>(null)
  const [playhead, setPlayhead] = useState(0)
  const [target, setTarget] = useState<number | null>(null)
  const [speed, setSpeed] = useState(1)
  const [busy, setBusy] = useState<Busy>('loading')
  const [error, setError] = useState<string | null>(null)
  const [hint, setHint] = useState<string | null>(null)
  const [attention, setAttention] = useState<Attention>(null)
  const [lastReplyMs, setLastReplyMs] = useState<number | null>(null)
  const [scenarioId, setScenarioId] = useState(DEFAULT_SCENARIO)
  const stateRef = useRef<DashboardState | null>(null)
  useLayoutEffect(() => {
    stateRef.current = state
  }, [state])
  // While our own request is in flight, its response carries the new state; the
  // WebSocket echo of the same change must not trigger a second (redundant) fetch.
  const mutating = useRef(0)
  // A different change (e.g. a live FaceTime or iMessage confirmation) can land while our
  // request is in flight. Remember the newest version announced meanwhile, and fetch it
  // after the request if our response did not already include it.
  const pendingUpdate = useRef<{ runId: string; version: number } | null>(null)

  const adopt = useCallback((next: DashboardState, resetPlayhead = false) => {
    const current = stateRef.current
    if (current && next.run.run_id === current.run.run_id && next.run.version < current.run.version) {
      return // stale response; never move backwards
    }
    stateRef.current = next // visible to the WebSocket handler before the next render
    setState(next)
    if (resetPlayhead || !current || current.run.run_id !== next.run.run_id) {
      setPlayhead(next.timeline.demo_window.start_tick)
      setTarget(null)
    }
  }, [])

  const fail = useCallback((e: unknown) => {
    setError(e instanceof Error ? e.message : 'Unexpected error')
  }, [])

  const endMutation = useCallback(() => {
    mutating.current -= 1
    if (mutating.current > 0) return
    const pending = pendingUpdate.current
    pendingUpdate.current = null
    const current = stateRef.current
    if (!pending || !current || pending.runId !== current.run.run_id) return
    if (pending.version > current.run.version) {
      api.run(current.run.run_id).then((s) => adopt(s)).catch(fail)
    }
  }, [adopt, fail])

  // Initial load: the active run (the presenter resets explicitly).
  useEffect(() => {
    let cancelled = false
    api
      .activeState()
      .then((s) => {
        if (cancelled) return
        adopt(s, true)
        setScenarioId(s.run.scenario.id)
      })
      .catch((e) => !cancelled && fail(e))
      .finally(() => !cancelled && setBusy(null))
    return () => {
      cancelled = true
    }
  }, [adopt, fail])

  // Live updates over WebSocket (reconnects with backoff).
  useEffect(() => {
    if (typeof WebSocket === 'undefined') return
    let socket: WebSocket | null = null
    let closed = false
    let delay = 500
    let timer: ReturnType<typeof setTimeout> | undefined
    const connect = () => {
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${window.location.host}/ws`)
      socket.onopen = () => {
        delay = 500
      }
      socket.onmessage = (event) => {
        let msg: { type?: string; run_id?: string; version?: number; reason?: string }
        try {
          msg = JSON.parse(String(event.data))
        } catch {
          return
        }
        if (msg.type !== 'run.updated') return
        if (mutating.current > 0) {
          const p = pendingUpdate.current
          if (msg.run_id && typeof msg.version === 'number' && (!p || p.runId !== msg.run_id || msg.version > p.version)) {
            pendingUpdate.current = { runId: msg.run_id, version: msg.version }
          }
          return
        }
        const current = stateRef.current
        if (!current) return
        if (msg.run_id !== current.run.run_id) {
          if (msg.reason === 'reset' || msg.reason === 'run_created') {
            api.activeState().then((s) => adopt(s, true)).catch(fail)
          }
          return
        }
        if ((msg.version ?? 0) > current.run.version) {
          api.run(current.run.run_id).then((s) => adopt(s)).catch(fail)
        }
      }
      socket.onclose = () => {
        if (closed) return
        timer = setTimeout(connect, delay)
        delay = Math.min(delay * 2, 8000)
      }
    }
    connect()
    return () => {
      closed = true
      clearTimeout(timer)
      socket?.close()
    }
  }, [adopt, fail])

  // Playback: move one tick at a time toward the target.
  const playing = target !== null && target !== playhead
  useEffect(() => {
    if (!state || target === null || playhead === target) return
    const ms = state.timeline.suggested_ms_per_tick / speed
    const timer = setTimeout(() => setPlayhead((p) => (p < target ? p + 1 : p - 1)), ms)
    return () => clearTimeout(timer)
  }, [state, target, playhead, speed])

  const flash = useCallback((message: string, what: Attention) => {
    setHint(message)
    setAttention(what)
    window.setTimeout(() => setAttention(null), 2700)
    window.setTimeout(() => setHint((h) => (h === message ? null : h)), 4000)
  }, [])

  const reset = useCallback(async (id?: string) => {
    setBusy('reset')
    setError(null)
    setHint(null)
    try {
      mutating.current += 1
      try {
        adopt(await api.reset(id ?? scenarioId, DEFAULT_SEED), true)
      } finally {
        endMutation()
      }
      setLastReplyMs(null)
    } catch (e) {
      fail(e)
    } finally {
      setBusy(null)
    }
  }, [adopt, endMutation, fail, scenarioId])

  const approve = useCallback(async () => {
    const s = stateRef.current
    const card = s && recommendedProposal(s)
    if (!s || !card || card.status === 'approved') return
    if (playhead < card.tick) {
      flash(`The outreach can be approved once it is proposed (${card.clock})`, null)
      return
    }
    setTarget(null)
    setBusy('approve')
    try {
      // The approval happens at the playhead: the 45-minute lead time starts here.
      mutating.current += 1
      try {
        adopt(await api.approve(s.run.run_id, card.id, playhead))
      } finally {
        endMutation()
      }
    } catch (e) {
      fail(e)
    } finally {
      setBusy(null)
    }
  }, [adopt, endMutation, fail, flash, playhead])

  const simulateReply = useCallback(async () => {
    const s = stateRef.current
    const req = s && activeRequest(s)
    if (!s || !req) return
    setBusy('reply')
    const started = performance.now()
    try {
      mutating.current += 1
      try {
        adopt(await api.simulateReply(s.run.run_id, req.request_id))
      } finally {
        endMutation()
      }
      setLastReplyMs(Math.round(performance.now() - started))
    } catch (e) {
      fail(e)
    } finally {
      setBusy(null)
    }
  }, [adopt, endMutation, fail])

  const nextEvent = useCallback(() => {
    const s = stateRef.current
    if (!s) return
    const action = nextAction(s, playhead)
    if (action.kind === 'advance') {
      setHint(null)
      setTarget(action.toTick)
    } else if (action.kind === 'await') {
      flash(action.reason, action.step.needs === 'approval' ? 'approve' : 'reply')
    }
  }, [flash, playhead])

  const togglePlay = useCallback(() => {
    const s = stateRef.current
    if (!s) return
    if (playing) {
      setTarget(null)
      return
    }
    const { last } = windowRange(s)
    setTarget(playhead >= last ? null : last)
  }, [playhead, playing])

  const scrub = useCallback((tick: number) => {
    const s = stateRef.current
    if (!s) return
    setTarget(null)
    setPlayhead(clampTick(s, tick))
  }, [])

  const stepBack = useCallback(() => {
    const s = stateRef.current
    if (!s) return
    setTarget(null)
    setPlayhead((p) => clampTick(s, p - 1))
  }, [])

  return {
    state,
    playhead,
    playing,
    speed,
    busy,
    error,
    hint,
    attention,
    lastReplyMs,
    next: state ? nextAction(state, playhead) : null,
    scenarioId,
    setScenarioId,
    reset,
    approve,
    simulateReply,
    nextEvent,
    togglePlay,
    scrub,
    stepBack,
    setSpeed,
    dismissError: () => setError(null),
  }
}
