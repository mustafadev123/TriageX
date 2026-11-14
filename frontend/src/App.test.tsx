import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

function mockFetch(impl: () => Promise<Response>) {
  vi.stubGlobal('fetch', vi.fn(impl))
}

afterEach(() => vi.unstubAllGlobals())

describe('App shell', () => {
  it('shows TriageX branding and the synthetic-data notice', async () => {
    mockFetch(async () => new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
    render(<App />)

    expect(screen.getByRole('heading', { name: 'TriageX' })).toBeInTheDocument()
    expect(screen.getByText(/TriageX calls the people who can fix it/)).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('not real-time hospital data')
    expect(await screen.findByText('Backend: online')).toBeInTheDocument()
  })

  it('shows a visible error when the backend is unreachable', async () => {
    mockFetch(async () => new Response('', { status: 503 }))
    render(<App />)

    expect(await screen.findByText('Backend: offline')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('HTTP 503')
  })
})
