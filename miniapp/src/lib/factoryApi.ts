/* Client for runtime/factory_analytics_api.py — the owner-only factory
   dashboard. Deliberately separate from lib/api.ts: that module's request()
   prefixes every call with /api/{bot_id} (derived from the URL path) because
   it talks to a single tenant bot's own data. This dashboard's routes live
   under the fixed /api/factory/ prefix instead (see that module's
   register_routes() docstring for why bot_id=0/"factory" isn't a normal
   tenant bot_id), so it needs its own thin fetch wrapper — but reuses the
   exact same two auth headers (X-Telegram-Init-Data / ?token=), since
   _authenticate_owner() on the backend is the same HMAC scheme as
   miniapp_api.py's _authenticate(), just additionally checked against
   OWNER_ID. */

import { getInitData } from './telegram'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function getMagicLinkToken(): string | null {
  return new URLSearchParams(window.location.search).get('token')
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const initData = getInitData()
  const headers = new Headers(options.headers)
  headers.set('Content-Type', 'application/json')

  let url = `/api/factory${path}`
  if (initData) {
    headers.set('X-Telegram-Init-Data', initData)
  } else {
    const token = getMagicLinkToken()
    if (token) {
      const separator = url.includes('?') ? '&' : '?'
      url = `${url}${separator}token=${encodeURIComponent(token)}`
    }
  }

  const response = await fetch(url, { ...options, headers })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body.error || `request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

export interface FactoryBotItem {
  id: number
  name: string
  username: string | null
  display_name: string | null
  status: string
  created_at: string
  archived_at: string | null
  template: string | null
  features: string[]
  edits_count: number
  avg_rating: number | null
  feedback_count: number
  weekly_count: number | null
}

export function listFactoryBots(): Promise<{ items: FactoryBotItem[] }> {
  return request('/bots')
}

export function addFactoryFeedback(
  botId: number,
  rating: number,
  comment?: string,
): Promise<{ ok: true }> {
  return request(`/bots/${botId}/feedback`, {
    method: 'POST',
    body: JSON.stringify({ rating, comment }),
  })
}

export interface TemplateCandidateItem {
  id: number
  bot_id: number | null
  bot_name: string | null
  summary: string
  fallback_reason: string
  selected_templates: string[]
  bot_type: string | null
  created_at: string
}

export function listTemplateCandidates(): Promise<{ items: TemplateCandidateItem[] }> {
  return request('/candidates')
}

export interface TemplateCandidateClusterItem {
  id: number
  label: string
  description: string | null
  count: number
  first_seen: string
  last_seen: string
  examples: string[]
}

export function listTemplateCandidateClusters(): Promise<{ items: TemplateCandidateClusterItem[] }> {
  return request('/candidate-clusters')
}
