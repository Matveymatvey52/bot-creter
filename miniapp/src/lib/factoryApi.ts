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

// The magic-link token minted by mint_magic_link_token() (runtime/
// miniapp_api.py) is deliberately short-lived (MAGIC_LINK_TTL_SECONDS = 15
// min) — a "click the link now" flow, not a durable session. That module's
// own docstring says the SPA is expected to exchange it for a longer-lived
// session on first load; until this, that exchange never happened, so the
// dashboard's OWN bot list (fetched once on mount) kept showing already-
// loaded data while every later click — bot detail, candidates, clusters —
// started failing with 403 the moment the 15-minute window closed. We now
// persist the token in sessionStorage (survives the SPA's own client-side
// navigation, gone when the tab closes — same "don't outlive this visit"
// posture as the original link) and proactively refresh it well inside the
// TTL via /api/factory/session, so a session that's still open keeps working.
const TOKEN_STORAGE_KEY = 'factory_dashboard_token'
const REFRESH_INTERVAL_MS = 5 * 60 * 1000 // 5 min — comfortably under the 15 min TTL

function getMagicLinkToken(): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get('token')
  if (fromUrl) {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, fromUrl)
    return fromUrl
  }
  return sessionStorage.getItem(TOKEN_STORAGE_KEY)
}

function setStoredToken(token: string): void {
  sessionStorage.setItem(TOKEN_STORAGE_KEY, token)
}

let refreshTimer: ReturnType<typeof setInterval> | null = null

function ensureRefreshLoop(): void {
  if (refreshTimer !== null || getInitData()) {
    // No refresh needed inside Telegram: initData carries no expiry here
    // (see miniapp_api.py's _verify_telegram_init_data) and is re-issued
    // fresh each time Telegram opens the WebApp.
    return
  }
  refreshTimer = setInterval(() => {
    if (!getMagicLinkToken()) return
    request<{ token: string }>('/session')
      .then((data) => setStoredToken(data.token))
      .catch(() => {
        // A failed refresh means the token already lapsed or auth is gone —
        // nothing to do here, the next real request will surface the error.
      })
  }, REFRESH_INTERVAL_MS)
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
  ensureRefreshLoop()

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

export function listFactoryBots(): Promise<{ items: FactoryBotItem[]; is_owner: boolean }> {
  return request('/bots')
}

// OwnerRegistryItem — the separate owner-wide registry (see
// OwnerRegistryScreen.tsx): every bot across every customer, with
// owner_telegram_id shown explicitly. Deliberately a smaller shape than
// FactoryBotItem (no features/edits/rating/weekly_count) — this screen is
// about "who owns what", not per-bot analytics, which "Моя фабрика" already
// covers.
export interface OwnerRegistryItem {
  id: number
  name: string
  username: string | null
  display_name: string | null
  status: string
  created_at: string
  template: string | null
  owner_telegram_id: number | null
}

export function listOwnerRegistry(): Promise<{ items: OwnerRegistryItem[] }> {
  return request('/owner-registry')
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

// ── Bot detail panel (level 2) — docs discussion "Детальная панель бота" ────

export interface FeatureThreadTurn {
  role: 'owner' | 'claude'
  text: string
}

export interface FeatureStatusItem {
  name: string
  state: 'on' | 'pending' | 'off'
  description: string | null
  thread: FeatureThreadTurn[]
  no_free_text: boolean
}

export interface OfficeLink {
  source_bot_id: number
  target_bot_id: number
  event_type: string
}

export interface BotDetail {
  id: number
  name: string
  username: string | null
  display_name: string | null
  status: string
  running: boolean
  template: string | null
  created_at: string
  admins: string[]
  offices: OfficeLink[]
  features: FeatureStatusItem[]
}

export function getBotDetail(botId: number): Promise<BotDetail> {
  return request(`/bots/${botId}`)
}

export function startBot(botId: number): Promise<{ ok: true; already_running?: boolean }> {
  return request(`/bots/${botId}/start`, { method: 'POST' })
}

export function stopBot(botId: number): Promise<{ ok: true }> {
  return request(`/bots/${botId}/stop`, { method: 'POST' })
}

export function restartBot(botId: number): Promise<{ ok: true }> {
  return request(`/bots/${botId}/restart`, { method: 'POST' })
}

export function deleteBot(botId: number): Promise<{ ok: true }> {
  return request(`/bots/${botId}`, { method: 'DELETE' })
}

export function getBotLogs(botId: number): Promise<{ logs: string }> {
  return request(`/bots/${botId}/logs`)
}

export function recreateBot(botId: number): Promise<{ ok: boolean; error: string | null; bot_name: string | null }> {
  return request(`/bots/${botId}/recreate`, { method: 'POST' })
}

export function autofixBot(botId: number): Promise<{ ok: boolean; error: string | null; bot_name: string | null }> {
  return request(`/bots/${botId}/autofix`, { method: 'POST' })
}

export function fixBug(
  botId: number,
  description: string,
): Promise<{ ok: boolean; error: string | null; bot_name: string | null }> {
  return request(`/bots/${botId}/fixbug`, { method: 'POST', body: JSON.stringify({ description }) })
}

export function listBotFeatures(botId: number): Promise<{ items: FeatureStatusItem[] }> {
  return request(`/bots/${botId}/features`)
}

export function disableFeature(botId: number, name: string): Promise<{ ok: true }> {
  return request(`/bots/${botId}/features/${name}/disable`, { method: 'POST' })
}

export interface ConfigureFeatureResult {
  status: 'enabled' | 'needs_clarification'
  reply: string
  description?: string
  thread?: FeatureThreadTurn[]
}

export function configureFeature(botId: number, name: string, message: string): Promise<ConfigureFeatureResult> {
  return request(`/bots/${botId}/features/${name}/configure`, {
    method: 'POST',
    body: JSON.stringify({ message }),
  })
}

export function cancelFeatureConfigure(botId: number, name: string): Promise<{ ok: true }> {
  return request(`/bots/${botId}/features/${name}/cancel`, { method: 'POST' })
}

export function listOffices(botId: number): Promise<{ items: OfficeLink[] }> {
  return request(`/bots/${botId}/offices`)
}

export interface OfficeEventTypeOption {
  event_type: string
  label: string
}

export function listOfficeEventTypes(botId: number): Promise<{ items: OfficeEventTypeOption[] }> {
  return request(`/bots/${botId}/offices/event-types`)
}

export function addOffice(botId: number, targetBotId: number, eventType: string): Promise<{ ok: true }> {
  return request(`/bots/${botId}/offices`, {
    method: 'POST',
    body: JSON.stringify({ target_bot_id: targetBotId, event_type: eventType }),
  })
}

export function removeOffice(botId: number, targetBotId: number, eventType: string): Promise<{ ok: true }> {
  return request(`/bots/${botId}/offices/${targetBotId}?event_type=${encodeURIComponent(eventType)}`, {
    method: 'DELETE',
  })
}

export function getShowcaseGroupStatus(): Promise<{ connected: boolean }> {
  return request('/showcase-group')
}

export function listAdmins(botId: number): Promise<{ items: string[] }> {
  return request(`/bots/${botId}/admins`)
}

export function addAdmin(botId: number, telegramId: string): Promise<{ ok: true }> {
  return request(`/bots/${botId}/admins`, {
    method: 'POST',
    body: JSON.stringify({ telegram_id: telegramId }),
  })
}

export function removeAdmin(botId: number, telegramId: string): Promise<{ ok: true }> {
  return request(`/bots/${botId}/admins/${telegramId}`, { method: 'DELETE' })
}

