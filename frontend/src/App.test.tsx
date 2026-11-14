import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import approved from './test/fixtures/approved.json'
import confirmed1 from './test/fixtures/confirmed1.json'
import confirmed2 from './test/fixtures/confirmed2.json'
import confirmed3 from './test/fixtures/confirmed3.json'
import initial from './test/fixtures/initial.json'

// Every response below is a real API payload exported by scripts/export_fixtures.py.
interface Call {
  method: string
  url: string
  body: unknown
}

let calls: Call[] = []
let replies: unknown[] = []

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
}

function mockApi() {
  calls = []
  replies = [confirmed1, confirmed2, confirmed3]
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      if (url === '/api/demo/state') return json(initial)
      if (url === '/api/demo/reset') return json(initial)
      if (url === '/api/scenarios') return json([{ id: 'i85_pileup', name: 'I-85 Multi-Vehicle Pileup' }])
      if (url.endsWith('/approve')) return json(approved)
      if (url.endsWith('/simulate-reply')) return json(replies.shift() ?? confirmed3)
      if (url === '/api/integrations/health') {
        return json({
          simulation: { name: 'Simulation engine', status: 'ok', detail: 'Deterministic, in-process' },
          database: { name: 'Tiger Data', status: 'not_configured', detail: 'DATABASE_URL is not set' },
          ai_primary: { name: 'OpenAI', status: 'not_implemented', detail: 'Arrives in Phase 5' },
          ai_fallback: { name: 'Gemini', status: 'not_implemented', detail: 'Arrives in Phase 5' },
          messaging: { name: 'Chert', status: 'not_implemented', detail: 'Arrives in Phase 6' },
          websocket_clients: 1,
        })
      }
      if (url === '/api/db/status') return json({ configured: false, connected: false, writer: {} })
      if (url === '/api/meta/provenance') return json({ commit: 'abc', entire_checkpoints: [] })
      if (url.endsWith('/diagnostics')) return json({ forecast_accuracy: {}, model_limitations: [] })
      return json({ detail: 'not mocked' }, 404)
    }),
  )
}

const posts = (suffix: string) => calls.filter((c) => c.method === 'POST' && c.url.endsWith(suffix))

async function renderApp() {
  render(<App />)
  await screen.findByText('Monitoring 5 emergency departments')
}

async function next(times = 1) {
  for (let i = 0; i < times; i++) {
    fireEvent.click(screen.getByTestId('next-event'))
    // Playback advances one tick per suggested_ms_per_tick; run the timers.
    for (let k = 0; k < 8; k++) {
      await act(async () => {
        vi.advanceTimersByTime(400)
      })
    }
  }
}

const clock = () => screen.getByTestId('clock').textContent
const hoursSaved = () => screen.getByTestId('hours-saved').textContent

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  mockApi()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('TriageX command center', () => {
  it('renders the synthetic notice, all five hospitals and a calm initial state', async () => {
    await renderApp()
    expect(screen.getByRole('note')).toHaveTextContent('not real-time hospital data')
    expect(screen.getByText(/TriageX calls the people who can fix it/)).toBeInTheDocument()
    expect(clock()).toBe('15:00')
    expect(hoursSaved()).toBe('0.00')
    expect(screen.getByTestId('next-event')).toHaveTextContent('Start Surge')
  })

  it('walks the presenter steps on real API timestamps', async () => {
    await renderApp()
    await next()
    expect(clock()).toBe('16:00')
    await next()
    expect(clock()).toBe('16:15')
    const action = screen.getByRole('region', { name: 'Coordination' })
    expect(within(action).getByText('Call in 3 staff at Grady')).toBeInTheDocument()
    expect(within(screen.getByTestId('feed')).getByText('Outreach Proposed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve outreach' })).toBeInTheDocument()
  })

  it('never approves on Next Event; approval is an explicit click at 16:30', async () => {
    await renderApp()
    await next(3)
    expect(clock()).toBe('16:30')
    await next() // pressing Next again only asks for the approval
    expect(posts('/approve')).toHaveLength(0)
    expect(screen.getByRole('status')).toHaveTextContent('Approve the outreach to continue')

    fireEvent.click(screen.getByRole('button', { name: 'Approve outreach' }))
    await waitFor(() => expect(posts('/approve')).toHaveLength(1))
    expect(posts('/approve')[0].body).toMatchObject({ at_tick: 18 })
    expect(await screen.findByTestId('confirmed-count')).toHaveTextContent('0/3')
    expect(screen.getByTestId('request-status')).toHaveTextContent('shift starts 17:15')
  })

  it('records three simulated replies through the backend without premature impact', async () => {
    await renderApp()
    await next(3)
    fireEvent.click(screen.getByRole('button', { name: 'Approve outreach' }))
    await screen.findByTestId('confirmed-count')

    for (const n of [1, 2, 3]) {
      fireEvent.click(screen.getByRole('button', { name: /Simulate staff reply/ }))
      await waitFor(() => expect(screen.getByTestId('confirmed-count')).toHaveTextContent(`${n}/3`))
    }
    expect(posts('/simulate-reply')).toHaveLength(3)
    expect(screen.getAllByText('Simulated').length).toBe(3)
    // Confirmed, but the shift has not started: no improvement may be shown yet.
    expect(clock()).toBe('16:30')
    expect(hoursSaved()).toBe('0.00')
    expect(screen.getByTestId('hero-caption')).toHaveTextContent('identical until the shift starts at 17:15')
    expect(screen.queryByTestId('shift-active-banner')).not.toBeInTheDocument()
    const feed = within(screen.getByTestId('feed'))
    expect(feed.getAllByText('Staff Confirmed')).toHaveLength(3)
    expect(feed.getAllByText('Simulated reply')).toHaveLength(3)
    expect(feed.queryByText('Live reply')).not.toBeInTheDocument()
  })

  it('reveals the divergence at 17:15 and the API final impact at the end', async () => {
    await renderApp()
    await next(3)
    fireEvent.click(screen.getByRole('button', { name: 'Approve outreach' }))
    await screen.findByTestId('confirmed-count')
    for (const n of [1, 2, 3]) {
      fireEvent.click(screen.getByRole('button', { name: /Simulate staff reply/ }))
      await waitFor(() => expect(screen.getByTestId('confirmed-count')).toHaveTextContent(`${n}/3`))
    }

    fireEvent.change(screen.getByLabelText('Simulation time'), { target: { value: '20' } })
    expect(clock()).toBe('17:00')
    expect(hoursSaved()).toBe('0.00')

    await next() // Activate Shift
    expect(clock()).toBe('17:15')
    expect(screen.getByTestId('shift-active-banner')).toHaveTextContent('SHIFT ACTIVE — GRADY')
    const at1715 = confirmed3.curves.points.find((p) => p.tick === 21)!.ambulance_hours_saved_so_far
    expect(hoursSaved()).toBe(at1715.toFixed(2))

    fireEvent.change(screen.getByLabelText('Simulation time'), { target: { value: '35' } })
    expect(clock()).toBe('20:45')
    expect(hoursSaved()).toBe((-confirmed3.impact.primary.delta).toFixed(2))
    expect(hoursSaved()).toBe('9.25')
    expect(screen.getByTestId('final-result')).toHaveTextContent('36% fewer')
  })

  it('reset calls the backend and returns to the initial state', async () => {
    await renderApp()
    await next(2)
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(posts('/api/demo/reset')).toHaveLength(1))
    expect(posts('/api/demo/reset')[0].body).toEqual({ scenario_id: 'i85_pileup', seed: 42 })
    await waitFor(() => expect(clock()).toBe('15:00'))
  })

  it('technical view reports integration status exactly as the API does', async () => {
    await renderApp()
    fireEvent.click(screen.getByRole('button', { name: 'Technical' }))
    const panel = await screen.findByRole('complementary', { name: 'Technical details' })
    await within(panel).findByText('not_configured')
    expect(within(panel).getAllByText('not_implemented')).toHaveLength(3)
    expect(within(panel).queryByText(/^ok$/)).toBeInTheDocument() // the simulation only
  })

  it('shows a visible error when the backend is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503 })))
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 503')
  })
})
