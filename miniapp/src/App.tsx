import { useEffect, useState } from 'react'
import './components/ui.css'
import { getSchema, ApiError } from './lib/api'
import { normalizeResources, type ResourceDisplay } from './lib/displaySchema'
import { getTelegramWebApp } from './lib/telegram'
import { ListScreen } from './screens/ListScreen'
import { DetailScreen } from './screens/DetailScreen'
import { CreateFormScreen } from './screens/CreateFormScreen'
import { FactoryDashboardScreen } from './screens/FactoryDashboardScreen'

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
        setRoute(firstName ? { kind: 'list', resource: firstName } : null)
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
      {route.kind !== 'list' ? null : (
        <div className="chip-row" style={{ padding: '16px 16px 0' }}>
          {resourceNames.map((name) => (
            <button
              key={name}
              className="chip"
              style={
                name === route.resource
                  ? { background: 'var(--accent)', color: 'var(--accent-text)' }
                  : undefined
              }
              onClick={() => setRoute({ kind: 'list', resource: name })}
            >
              {resources[name].title}
            </button>
          ))}
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
    </div>
  )
}
