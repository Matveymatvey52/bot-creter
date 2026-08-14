/* Client for runtime/miniapp_api.py's REST layer. Mirrors that module's
   exact contract: GET /api/{bot_id}/me, GET/POST /api/{bot_id}/{resource},
   GET /api/{bot_id}/{resource}/{item_id} — see that file's docstring for the
   auth scheme this implements both sides of. */

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

function botIdFromPath(): string {
  // Path is /app/{bot_id}[/...] — see runtime/combined_app.py's route and
  // the SPA's own router in App.tsx, which both key off this same segment.
  const match = window.location.pathname.match(/\/app\/(\d+)/)
  if (!match) {
    throw new Error('bot_id missing from URL path (expected /app/{bot_id})')
  }
  return match[1]
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const botId = botIdFromPath()
  const initData = getInitData()
  const headers = new Headers(options.headers)
  headers.set('Content-Type', 'application/json')

  let url = `/api/${botId}${path}`
  if (initData) {
    // Telegram WebView context — see miniapp_api.py's _authenticate(),
    // which tries this header before falling back to ?token=.
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

export interface ResourceItem {
  id: number
  [key: string]: unknown
}

export function listResource(resource: string): Promise<{ resource: string; items: ResourceItem[] }> {
  return request(`/${resource}`)
}

export function getResource(resource: string, itemId: number): Promise<{ resource: string; item: ResourceItem }> {
  return request(`/${resource}/${itemId}`)
}

export function createResource(
  resource: string,
  payload: Record<string, unknown>,
): Promise<{ resource: string; id: number }> {
  return request(`/${resource}`, { method: 'POST', body: JSON.stringify(payload) })
}

export function getMe(): Promise<{ bot_id: number; telegram_user_id: number }> {
  return request('/me')
}
