/* Client for runtime/owner_report_api.py — the SYSTEM OWNER's cross-owner
   report (Stage 2 of the multitenancy rollout). Deliberately a SEPARATE thin
   fetch wrapper from both lib/api.ts (per-tenant-bot routes) and
   lib/factoryApi.ts (the per-owner "Моя фабрика" dashboard, /api/factory/...) —
   this module's routes live under their own /api/owner-report/ prefix and are
   unconditionally OWNER_ID-only (no per-customer variant), same auth headers
   (X-Telegram-Init-Data / ?token=) as factoryApi.ts reuses from miniapp_api.py. */

import { getInitData } from './telegram'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// Same short-lived-magic-link-token-in-sessionStorage posture as
// factoryApi.ts's own TOKEN_STORAGE_KEY — kept as an independent key so the
// two apps' sessions never collide if both are open in different tabs.
const TOKEN_STORAGE_KEY = 'owner_report_token'

function getMagicLinkToken(): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get('token')
  if (fromUrl) {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, fromUrl)
    return fromUrl
  }
  return sessionStorage.getItem(TOKEN_STORAGE_KEY)
}

async function request<T>(path: string): Promise<T> {
  const initData = getInitData()
  const headers = new Headers()
  headers.set('Content-Type', 'application/json')

  let url = `/api/owner-report${path}`
  if (initData) {
    headers.set('X-Telegram-Init-Data', initData)
  } else {
    const token = getMagicLinkToken()
    if (token) {
      const separator = url.includes('?') ? '&' : '?'
      url = `${url}${separator}token=${encodeURIComponent(token)}`
    }
  }

  const response = await fetch(url, { headers })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body.error || `request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

export interface OwnerReportBotItem {
  id: number
  name: string
  username: string | null
  display_name: string | null
  status: string
  created_at: string
  archived_at: string | null
  owner_telegram_id: number | null
  owner_display: string | null
  creation_prompt: string | null
  template: string | null
  features: string[]
  edits_count: number
  avg_rating: number | null
  feedback_count: number
  payments_connected: boolean
  last_activity_at: string | null
  approx_data_volume_bytes: number | null
}

export function listOwnerReportBots(): Promise<{ items: OwnerReportBotItem[] }> {
  return request('/bots')
}

export interface OwnerReportActivityItem {
  source: 'office_event' | 'feedback' | 'payment'
  bot_id: number
  bot_name: string
  owner_telegram_id: number | null
  telegram_user_id: number | null
  event_type: string
  detail: string | null
  created_at: string
}

export interface OwnerReportActivityFilters {
  ownerId?: number
  botId?: number
  limit?: number
  offset?: number
}

export function listOwnerReportActivity(
  filters: OwnerReportActivityFilters = {},
): Promise<{ items: OwnerReportActivityItem[]; total: number; limit: number; offset: number }> {
  const params = new URLSearchParams()
  if (filters.ownerId != null) params.set('owner_id', String(filters.ownerId))
  if (filters.botId != null) params.set('bot_id', String(filters.botId))
  if (filters.limit != null) params.set('limit', String(filters.limit))
  if (filters.offset != null) params.set('offset', String(filters.offset))
  const qs = params.toString()
  return request(`/activity${qs ? `?${qs}` : ''}`)
}

// Moved here from factoryApi.ts (owner instruction, 2026-08-18) — these two
// sections never showed per-owner data, they belong to the cross-owner
// report, not "Моя фабрика".

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
