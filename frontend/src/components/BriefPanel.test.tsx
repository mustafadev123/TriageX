import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import approved from '../test/fixtures/approved.json'
import type { DashboardState, OperationalBrief } from '../types/dashboard'

vi.mock('../services/api', () => ({ api: { brief: vi.fn() } }))
const { api } = await import('../services/api')
const { BriefPanel } = await import('./BriefPanel')

const brief = (over: Partial<OperationalBrief>): OperationalBrief => ({
  brief_id: 'b', run_id: 'r', version: 2, at_tick: 18, clock: '16:30', provider: 'gemini',
  model: 'gemini-3.1-flash-lite', latency_ms: 900, fallback_reason: null,
  headline: 'Grady at 100% occupancy.', bullets: ['Forecast 187%.'], action: 'Collect confirmations.',
  metric_keys: [], facts: {}, generated_at: '', notice: 'Synthetic simulation data.', attempts: [], ...over,
})

describe('BriefPanel', () => {
  it('asks for a brief at the playhead and shows the provider badge', async () => {
    const state = approved as unknown as DashboardState
    vi.mocked(api.brief).mockResolvedValue(brief({ run_id: state.run.run_id }))
    render(<BriefPanel state={state} playhead={18} />)
    await userEvent.click(screen.getByRole('button', { name: /Generate \(16:30\)/ }))
    expect(api.brief).toHaveBeenCalledWith(state.run.run_id, 18)
    expect(await screen.findByText('Grady at 100% occupancy.')).toBeInTheDocument()
    expect(screen.getByText('Gemini')).toBeInTheDocument()
    expect(screen.queryByTestId('brief-fallback')).toBeNull()
  })

  it('never hides a fallback', async () => {
    vi.mocked(api.brief).mockResolvedValue(
      brief({
        run_id: (approved as unknown as DashboardState).run.run_id,
        provider: 'template',
        model: null,
        fallback_reason: 'primary p: timeout; fallback f: rate_limited',
      }),
    )
    render(<BriefPanel state={approved as unknown as DashboardState} playhead={18} />)
    await userEvent.click(screen.getByRole('button', { name: /Generate/ }))
    expect(await screen.findByText('Template · AI unavailable')).toBeInTheDocument()
    expect(screen.getByTestId('brief-fallback')).toHaveTextContent('primary p: timeout; fallback f: rate_limited')
  })
})
