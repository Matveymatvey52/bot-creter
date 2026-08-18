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

export function addOffice(botId: number, targetBotId: number): Promise<{ ok: true }> {
  return request(`/bots/${botId}/offices`, {
    method: 'POST',
    body: JSON.stringify({ target_bot_id: targetBotId }),
  })
}

export function removeOffice(botId: number, targetBotId: number): Promise<{ ok: true }> {
  return request(`/bots/${botId}/offices/${targetBotId}`, { method: 'DELETE' })
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
