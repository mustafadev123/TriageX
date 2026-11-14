import type { DashboardState, IntegrationsResponse, ScenarioInfo } from '../types/dashboard'

export interface HealthResponse {
  status: string
  app: string
  tagline: string
  notice: string
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // keep the status-only message
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

export function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health', { signal })
}

export const api = {
  scenarios: () => request<ScenarioInfo[]>('/api/scenarios'),
  reset: (scenarioId: string, seed: number) =>
    request<DashboardState>('/api/demo/reset', {
      method: 'POST',
      body: JSON.stringify({ scenario_id: scenarioId, seed }),
    }),
  activeState: () => request<DashboardState>('/api/demo/state'),
  run: (runId: string) => request<DashboardState>(`/api/runs/${encodeURIComponent(runId)}`),
  approve: (runId: string, proposalId: string, atTick: number) =>
    request<DashboardState>(
      `/api/runs/${encodeURIComponent(runId)}/proposals/${encodeURIComponent(proposalId)}/approve`,
      { method: 'POST', body: JSON.stringify({ at_tick: atTick, operator: 'demo-operator' }) },
    ),
  simulateReply: (runId: string, requestId: string) =>
    request<DashboardState>(
      `/api/runs/${encodeURIComponent(runId)}/staffing-requests/${encodeURIComponent(requestId)}/simulate-reply`,
      { method: 'POST', body: JSON.stringify({ reply: 'IN' }) },
    ),
  integrations: () => request<IntegrationsResponse>('/api/integrations/health'),
  dbStatus: () => request<Record<string, unknown>>('/api/db/status'),
  dbInsights: (runId: string) =>
    request<Record<string, unknown>>(`/api/runs/${encodeURIComponent(runId)}/db/insights`),
  diagnostics: (runId: string) =>
    request<Record<string, unknown>>(`/api/runs/${encodeURIComponent(runId)}/diagnostics`),
  provenance: () => request<Record<string, unknown>>('/api/meta/provenance'),
}
