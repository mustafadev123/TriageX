import { useEffect, useState } from 'react'
import { fetchHealth } from './services/api'
import { APP_NAME, SYNTHETIC_DATA_NOTICE, TAGLINE } from './types/branding'

type BackendState = 'checking' | 'online' | 'offline'

const BACKEND_LABEL: Record<BackendState, string> = {
  checking: 'Backend: checking…',
  online: 'Backend: online',
  offline: 'Backend: offline',
}

const BACKEND_COLOR: Record<BackendState, string> = {
  checking: 'bg-status-pressure',
  online: 'bg-status-normal',
  offline: 'bg-status-breach',
}

export default function App() {
  const [backend, setBackend] = useState<BackendState>('checking')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetchHealth(controller.signal)
      .then(() => setBackend('online'))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return
        setBackend('offline')
        setError(err instanceof Error ? err.message : 'Unknown error')
      })
    return () => controller.abort()
  }, [])

  return (
    <div className="min-h-screen">
      <div
        role="note"
        className="bg-status-pressure/15 px-4 py-1.5 text-center text-sm text-status-pressure"
      >
        {SYNTHETIC_DATA_NOTICE}
      </div>
      <header className="flex items-center justify-between border-b border-border bg-panel px-6 py-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{APP_NAME}</h1>
          <p className="text-sm text-slate-400">{TAGLINE}</p>
        </div>
        <div className="flex items-center gap-2 rounded-full border border-border px-3 py-1 text-sm">
          <span className={`h-2 w-2 rounded-full ${BACKEND_COLOR[backend]}`} />
          <span>{BACKEND_LABEL[backend]}</span>
        </div>
      </header>
      <main className="p-6">
        {error && (
          <p role="alert" className="mb-4 rounded border border-status-breach/50 px-3 py-2 text-sm">
            {error}
          </p>
        )}
        <p className="text-slate-400">
          Repository setup complete. The command-center dashboard is built in Phase 7.
        </p>
      </main>
    </div>
  )
}
