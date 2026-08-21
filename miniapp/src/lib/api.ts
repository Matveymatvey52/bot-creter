/* Client for runtime/miniapp_api.py's REST layer. Mirrors that module's
   exact contract: GET /api/{bot_id}/me, GET/POST /api/{bot_id}/{resource},
   GET /api/{bot_id}/{resource}/{item_id} — see that file's docstring for the
   auth scheme this implements both sides of. */

import { getInitData } from './telegram'

const TOKEN_STORAGE_PREFIX = 'site_token_'
const REFRESH_INTERVAL_MS = 60 * 60 * 1000 // 1h — comfortably under SITE_LINK_TTL_SECONDS (24h)

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// /site/{bot_id} (standalone website, Task B) persists its magic-link token
// in sessionStorage the same way lib/factoryApi.ts does for /app/0 — a
// bookmarked/reopened /site/{bot_id} tab with no ?token= in the URL (e.g.
// after client-side navigation) still needs to keep sending SOME token.
// /app/{bot_id} (Telegram WebView) never needed this: initData is always
// freshly supplied by Telegram on every open, so it never had to persist a
// token cross-navigation like /site/ does.
function isSitePath(): boolean {
  return /^\/site\//.test(window.location.pathname)
}

function getMagicLinkToken(botId: string): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get('token')
  if (fromUrl) {
    if (isSitePath()) sessionStorage.setItem(TOKEN_STORAGE_PREFIX + botId, fromUrl)
    return fromUrl
  }
  if (isSitePath()) return sessionStorage.getItem(TOKEN_STORAGE_PREFIX + botId)
  return null
}

function botIdFromPath(): string {
  // Path is /app/{bot_id}[/...] or /site/{bot_id}[/...] — see
  // runtime/combined_app.py's routes and the SPA's own router in App.tsx,
  // which both key off this same segment.
  const match = window.location.pathname.match(/\/(?:app|site)\/(\d+)/)
  if (!match) {
    throw new Error('bot_id missing from URL path (expected /app/{bot_id} or /site/{bot_id})')
  }
  return match[1]
}

let refreshTimer: ReturnType<typeof setInterval> | null = null

function ensureRefreshLoop(botId: string): void {
  if (refreshTimer !== null || !isSitePath() || getInitData()) return
  refreshTimer = setInterval(() => {
    const token = getMagicLinkToken(botId)
    if (!token) return
    request<{ token: string }>('/session')
      .then((data) => sessionStorage.setItem(TOKEN_STORAGE_PREFIX + botId, data.token))
      .catch(() => {
        // A failed refresh means the token already lapsed or auth is gone —
        // nothing to do here, the next real request will surface the error.
      })
  }, REFRESH_INTERVAL_MS)
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
    const token = getMagicLinkToken(botId)
    if (token) {
      const separator = url.includes('?') ? '&' : '?'
      url = `${url}${separator}token=${encodeURIComponent(token)}`
    }
  }
  ensureRefreshLoop(botId)

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

// Sub-records the backend joined onto a detail response — a record's own
// program/guests/attachments/payments, so the detail screen shows them in
// place instead of making the user hunt for them in a sibling tab. Absent on
// resources that declare no `children` (older configs included).
export interface RelatedSection {
  resource: string
  title: string
  as: 'table' | 'list'
  items: ResourceItem[]
}

export function getResource(
  resource: string,
  itemId: number,
): Promise<{ resource: string; item: ResourceItem; related?: RelatedSection[] }> {
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

// A foreign-key field: it stores another resource's id, but the user must
// never see or type that id (see the create form's picker). `labelField` is
// the human-readable column to show instead.
export interface SchemaRef {
  resource: string
  labelField: string
}

export interface SchemaField {
  name: string
  required?: boolean
  creatable?: boolean
  label?: string
  kind?: 'text' | 'number' | 'date' | 'status' | 'link' | 'file' | 'contact'
  list?: boolean
  detail?: boolean
  create?: boolean
  ref?: SchemaRef
}

export interface SchemaChild {
  resource: string
  via: string
  title?: string
  as?: 'table' | 'list'
}

export interface SchemaResource {
  name: string
  creatable?: boolean
  // Whether POST would actually succeed for THIS viewer — the backend's own
  // verdict (miniapp_api.py's _can_create), not something inferable client
  // side. Drives whether any create affordance is rendered at all.
  canCreate?: boolean
  title?: string
  titleField?: string
  tableView?: boolean
  children?: SchemaChild[]
  fields: SchemaField[]
}

/* Кто этот бот — для шапки приложения. Оба поля берутся из строки владельца
   в таблице bots и вполне могут быть пустыми (описание никто не обязан
   заполнять), поэтому nullable, а не пустые строки: шапка сама решает, что
   рисовать, а `bot` целиком отсутствует у ответов старого бэкенда. */
export interface SchemaBot {
  name: string | null
  subtitle: string | null
}

export function getSchema(): Promise<{ resources: SchemaResource[]; bot?: SchemaBot }> {
  return request('/schema')
}

// Optional features the bot's owner actually enabled (miniapp_api.py's
// features_handler). Sections like analytics are opt-in: absent from the
// navigation unless the corresponding feature is switched on for this bot.
export function getFeatures(): Promise<{ features: string[] }> {
  return request('/features')
}

export type AnalyticsPeriod = 'week' | 'month' | 'quarter'

export interface AnalyticsVolumePoint {
  date: string
  count: number
}

export interface AnalyticsTopCustomer {
  match_field: string | number
  count: number
}

export interface AnalyticsData {
  total: number
  volume: AnalyticsVolumePoint[]
  delta_pct: number | null
  top_customers: AnalyticsTopCustomer[]
  new_vs_returning: { new: number; returning: number } | null
}

// Mirrors runtime/miniapp_api.py's analytics_handler — owner-only (a 403
// here, unlike every other call in this file, can mean "authenticated but
// not this bot's owner", not just "not authenticated at all") and 404s when
// the bot has no usable office_hook_config (see that handler's docstring),
// which the caller (screens/AnalyticsScreen.tsx) treats as "hide this
// section" rather than an error to display.
export function getAnalytics(period: AnalyticsPeriod): Promise<AnalyticsData> {
  return request(`/analytics?period=${period}`)
}
