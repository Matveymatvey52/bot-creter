import { useEffect, useState } from 'react'
import './components/ui.css'
import { RESOURCES } from './lib/resources'
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

const RESOURCE_NAMES = Object.keys(RESOURCES)

export default function App() {
  const [route, setRoute] = useState<Route>({ kind: 'list', resource: RESOURCE_NAMES[0] })

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

  return (
    <div>
      {route.kind !== 'list' ? null : (
        <div className="chip-row" style={{ padding: '16px 16px 0' }}>
          {RESOURCE_NAMES.map((name) => (
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
              {RESOURCES[name].title}
            </button>
          ))}
        </div>
      )}

      {route.kind === 'list' && (
        <ListScreen
          resourceName={route.resource}
          onOpenItem={(itemId) => setRoute({ kind: 'detail', resource: route.resource, itemId })}
          onCreateNew={() => setRoute({ kind: 'create', resource: route.resource })}
        />
      )}

      {route.kind === 'detail' && (
        <DetailScreen
          resourceName={route.resource}
          itemId={route.itemId}
          onBack={() => setRoute({ kind: 'list', resource: route.resource })}
        />
      )}

      {route.kind === 'create' && (
        <CreateFormScreen
          resourceName={route.resource}
          onCreated={(itemId) => setRoute({ kind: 'detail', resource: route.resource, itemId })}
          onCancel={() => setRoute({ kind: 'list', resource: route.resource })}
        />
      )}
    </div>
  )
}
