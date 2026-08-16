import { useEffect, useState } from 'react'
import './components/ui.css'
import { getSchema, ApiError } from './lib/api'
import { normalizeResources, type ResourceDisplay } from './lib/displaySchema'
import { getTelegramWebApp } from './lib/telegram'
import { ListScreen } from './screens/ListScreen'
import { DetailScreen } from './screens/DetailScreen'
import { CreateFormScreen } from './screens/CreateFormScreen'
import { FactoryDashboardScreen } from './screens/FactoryDashboardScreen'
import { AnalyticsScreen } from './screens/AnalyticsScreen'

// bot_id=0 is the reserved FACTORY_BOT_ID (runtime/registry.py) — the
// factory's own /app command opens /app/0, which has no per-bot
// miniapp_config/resources of its own and instead gets the owner-only
// analytics dashboard (runtime/factory_analytics_api.py) rather than the
// generic per-bot resource list/detail/create routes below.
function isFactoryBotPath(): boolean {
  return /\/app\/0(\/|$)/.test(window.location.pathname)
}

type Route =
  | { kind: 'list'; resource: string }
  | { kind: 'detail'; resource: string; itemId: number }
  | { kind: 'create'; resource: string }
  | { kind: 'analytics' }

export default function App() {
  // Telegram's own bootstrap sequence — no-op outside the WebView (see
  // lib/telegram.ts: getTelegramWebApp() returns null in a plain browser).
  useEffect(() => {
    const webApp = getTelegramWebApp()
    webApp?.ready()
    webApp?.expand()
  }, [])

  if (isFactoryBotPath()) {
    return <FactoryDashboardScreen />
  }

  return <TenantApp />
}

// Split out from App() so the factory-dashboard branch above never mounts
// the schema fetch below — bot_id=0 has no miniapp_config/GET /schema route
// (see runtime/miniapp_api.py's serve_app_shell docstring on the same
// special-case), so nothing here should even try.
function TenantApp() {
  const [resources, setResources] = useState<Record<string, ResourceDisplay> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [route, setRoute] = useState<Route | null>(null)

  useEffect(() => {
    let cancelled = false
    getSchema()
      .then((data) => {
        if (cancelled) return
        const normalized = normalizeResources(data.resources)
        setResources(normalized)
        const firstName = Object.keys(normalized)[0]
        // A bot can have office_hook_config (analytics available) with NO
        // exposed CRUD resources at all — miniapp_config/office_hook_config
        // are independently configured (see runtime/miniapp_api.py's
        // serve_app_shell docstring on that same orthogonality). Falling
        // back to `null` here used to strand the UI on "Загрузка…" forever
        // (no resource tab to land on, and the analytics tab itself only
        // renders once `route` is non-null) — landing on the analytics
        // route instead keeps the app usable for exactly that bot shape.
        setRoute(firstName ? { kind: 'list', resource: firstName } : { kind: 'analytics' })
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить схему мини-приложения')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error) {
    return <div className="state-message">{error}</div>
  }
  if (!resources || !route) {
    return <div className="state-message">Загрузка…</div>
  }

  const resourceNames = Object.keys(resources)

  return (
    <div>
      {(route.kind === 'list' || route.kind === 'analytics') && (
        <div className="chip-row" style={{ padding: '16px 16px 0' }}>
          {resourceNames.map((name) => (
            <button
              key={name}
              className="chip"
              style={
                route.kind === 'list' && name === route.resource
                  ? { background: 'var(--accent)', color: 'var(--accent-text)' }
                  : undefined
              }
              onClick={() => setRoute({ kind: 'list', resource: name })}
            >
              {resources[name].title}
            </button>
          ))}
          {/* Always rendered — analytics_handler is the one that decides per-
              bot/per-viewer availability (owner-only, needs office_hook_config);
              AnalyticsScreen renders nothing (returns null) when it 403s/404s,
              so a non-owner or unsupported bot just sees an inert tab that
              opens an empty screen rather than a tab that's conspicuously
              missing (which would itself leak "you're not the owner"). */}
          <button
            className="chip"
            style={route.kind === 'analytics' ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
            onClick={() => setRoute({ kind: 'analytics' })}
          >
            Аналитика
          </button>
        </div>
      )}

      {route.kind === 'list' && (
        <ListScreen
          resource={resources[route.resource]}
          onOpenItem={(itemId) => setRoute({ kind: 'detail', resource: route.resource, itemId })}
          onCreateNew={() => setRoute({ kind: 'create', resource: route.resource })}
        />
      )}

      {route.kind === 'detail' && (
        <DetailScreen
          resource={resources[route.resource]}
          itemId={route.itemId}
          onBack={() => setRoute({ kind: 'list', resource: route.resource })}
        />
      )}

      {route.kind === 'create' && (
        <CreateFormScreen
          resource={resources[route.resource]}
          onCreated={(itemId) => setRoute({ kind: 'detail', resource: route.resource, itemId })}
          onCancel={() => setRoute({ kind: 'list', resource: route.resource })}
        />
      )}

      {route.kind === 'analytics' && <AnalyticsScreen />}
    </div>
  )
}
