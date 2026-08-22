import { useEffect, useState } from 'react'
import './components/ui.css'
import { getSchema, getFeatures, listResource, ApiError, type SchemaBot } from './lib/api'
import { AppHeader } from './components/AppHeader'
import { normalizeResources, type FieldDisplay, type ResourceDisplay } from './lib/displaySchema'
import { getTelegramWebApp } from './lib/telegram'
import { Icon, iconForResource } from './components/Icon'
import { ListScreen } from './screens/ListScreen'
import { DetailScreen } from './screens/DetailScreen'
import { CreateFormScreen } from './screens/CreateFormScreen'
import { MenuScreen } from './screens/MenuScreen'
import { FileScreen } from './screens/FileScreen'
import { FactoryDashboardScreen } from './screens/FactoryDashboardScreen'
import { AnalyticsScreen } from './screens/AnalyticsScreen'
import { OwnerReportScreen } from './screens/OwnerReportScreen'

// bot_id=0 is the reserved FACTORY_BOT_ID (runtime/registry.py) — the
// factory's own /app command opens /app/0, which has no per-bot
// miniapp_config/resources of its own and instead gets the owner-only
// analytics dashboard (runtime/factory_analytics_api.py) rather than the
// generic per-bot resource list/detail/create routes below.
function isFactoryBotPath(): boolean {
  return /\/app\/0(\/|$)/.test(window.location.pathname)
}

// /owner-report is a THIRD, fully separate app (Stage 2 of the
// multitenancy rollout, runtime/owner_report_api.py) — a system-owner-only
// cross-owner report, its own top-level route, not a tab inside either the
// per-bot customer app or the owner's own /app/0 "Моя фабрика" dashboard.
// Not linked from either of those apps' navigation on purpose (see that
// module's docstring).
function isOwnerReportPath(): boolean {
  return /^\/owner-report(\/|$)/.test(window.location.pathname)
}

type Route =
  | { kind: 'list'; resource: string }
  | { kind: 'detail'; resource: string; itemId: number }
  | { kind: 'create'; resource: string }
  | { kind: 'analytics' }
  // «Все разделы» — экран за кнопкой «⋯» в шапке (вариант I).
  | { kind: 'menu' }
  // Один файл записи: поле и его значение уже загружены деталкой, повторно
  // ходить за записью незачем.
  | { kind: 'file'; field: FieldDisplay; value: unknown; back: Route }

export default function App() {
  // Telegram's own bootstrap sequence — no-op outside the WebView (see
  // lib/telegram.ts: getTelegramWebApp() returns null in a plain browser).
  useEffect(() => {
    const webApp = getTelegramWebApp()
    webApp?.ready()
    webApp?.expand()
  }, [])

  if (isOwnerReportPath()) {
    return <OwnerReportScreen />
  }

  if (isFactoryBotPath()) {
    return <FactoryDashboardScreen />
  }

  return <TenantApp />
}

// Split out from App() so the factory-dashboard branch above never mounts
// the schema fetch below — bot_id=0 has no miniapp_config/GET /schema route
// (see runtime/miniapp_api.py's serve_app_shell docstring on the same
// special-case), so nothing here should even try.
// Sections beyond the bot's own resources are opt-in: each is listed here
// with the feature that must be enabled for the bot before it appears in the
// navigation at all. Analytics used to be an unconditional tab on every bot,
// which put a cross-customer sales screen in front of every client of every
// template whether or not the owner ever asked for one.
const ANALYTICS_FEATURE = 'sales_analytics'

function TenantApp() {
  const [resources, setResources] = useState<Record<string, ResourceDisplay> | null>(null)
  const [features, setFeatures] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [route, setRoute] = useState<Route | null>(null)
  // Счётчик записей под названием раздела в ленте (вариант I). Схема их не
  // отдаёт, поэтому считаем сами — по одному запросу на раздел, параллельно.
  // Раздел, который не отдался, просто остаётся без счётчика: подпись для
  // навигации второстепенна и не должна ломать экран.
  const [counts, setCounts] = useState<Record<string, number>>({})
  // Кто этот бот — для шапки (вариант I). Приходит тем же /schema,
  // отдельного запроса не нужно.
  const [bot, setBot] = useState<SchemaBot | null>(null)

  useEffect(() => {
    let cancelled = false
    getFeatures()
      .then((data) => {
        if (!cancelled) setFeatures(data.features)
      })
      // A failed feature lookup means "no optional sections", never a broken
      // app — the resource tabs below don't depend on it.
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    getSchema()
      .then((data) => {
        if (cancelled) return
        const normalized = normalizeResources(data.resources)
        setBot(data.bot ?? null)
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

  useEffect(() => {
    if (!resources) return
    let cancelled = false
    const names = Object.keys(resources)
    Promise.all(
      names.map((name) =>
        listResource(name)
          .then((data) => [name, data.items.length] as const)
          .catch(() => [name, -1] as const),
      ),
    ).then((pairs) => {
      if (cancelled) return
      setCounts(Object.fromEntries(pairs.filter(([, n]) => n >= 0)))
    })
    return () => {
      cancelled = true
    }
  }, [resources])

  if (error) {
    return <div className="state-message">{error}</div>
  }
  if (!resources || !route) {
    return <div className="state-message">Загрузка…</div>
  }

  const resourceNames = Object.keys(resources)
  // Нижняя навигация — первые три раздела плюс «Аналитика» (вариант I).
  // Лента выше остаётся полным списком разделов; нижняя панель — быстрый
  // доступ к тем, куда ходят чаще всего.
  const navNames = resourceNames.slice(0, 3)

  const activeResource =
    route.kind === 'list' || route.kind === 'detail' || route.kind === 'create'
      ? route.resource
      : null

  // Куда ведёт «назад» в шапке. На корневом экране (список раздела) возвращать
  // некуда — кнопка не рисуется вовсе, вместо неё пустой отступ.
  const backRoute: Route | null =
    route.kind === 'detail' || route.kind === 'create'
      ? { kind: 'list', resource: route.resource }
      : route.kind === 'file'
        ? route.back
        : route.kind === 'menu'
          ? { kind: 'list', resource: resourceNames[0] ?? '' }
          : null

  // «Аналитика» показывается, только если владелец реально подключил фичу
  // этому боту. analytics_handler по-прежнему сам решает, кому отдавать
  // данные (только владельцу) — здесь решается лишь навигация.
  const analyticsEnabled = features.includes(ANALYTICS_FEATURE)

  return (
    <>
      <AppHeader
        bot={bot}
        onBack={backRoute ? () => setRoute(backRoute) : undefined}
        onMenu={resourceNames.length > 0 ? () => setRoute({ kind: 'menu' }) : undefined}
      />

      {/* Лента разделов — свайп по горизонтали, а не ряд обособленных
          pill-кнопок: раскладка варианта I, утверждённая владельцем
          (design/mockups/miniapp_mockup_I.html). */}
      {(route.kind === 'list' || route.kind === 'analytics') && (
        <div className="sol-lane">
          {resourceNames.map((name) => (
            <button
              key={name}
              className={`sol-lane-item${route.kind === 'list' && name === route.resource ? ' is-on' : ''}`}
              onClick={() => setRoute({ kind: 'list', resource: name })}
            >
              <Icon name={iconForResource(name, resources[name].title)} size={17} />
              <div className="sol-lane-name">{resources[name].title}</div>
              <div className="sol-lane-count">
                {counts[name] === undefined ? '\u00A0' : `записей: ${counts[name]}`}
              </div>
            </button>
          ))}
          {analyticsEnabled && (
            <button
              className={`sol-lane-item${route.kind === 'analytics' ? ' is-on' : ''}`}
              onClick={() => setRoute({ kind: 'analytics' })}
            >
              <Icon name="chart" size={17} />
              <div className="sol-lane-name">Аналитика</div>
              <div className="sol-lane-count">&nbsp;</div>
            </button>
          )}
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
          resources={resources}
          itemId={route.itemId}
          onBack={() => setRoute({ kind: 'list', resource: route.resource })}
          onOpenRef={(refResource, itemId) =>
            setRoute({ kind: 'detail', resource: refResource, itemId })
          }
          onOpenChild={(childResource) => setRoute({ kind: 'list', resource: childResource })}
          onOpenFile={(field, value) => setRoute({ kind: 'file', field, value, back: route })}
        />
      )}

      {/* Second half of the read-only rule: even if a create route is reached
          some other way, a resource this viewer cannot write never renders a
          form. The list screen already hides the button that leads here. */}
      {route.kind === 'create' && resources[route.resource].canCreate && (
        <CreateFormScreen
          resource={resources[route.resource]}
          onCreated={(itemId) => setRoute({ kind: 'detail', resource: route.resource, itemId })}
          onCancel={() => setRoute({ kind: 'list', resource: route.resource })}
        />
      )}

      {route.kind === 'analytics' && <AnalyticsScreen />}

      {route.kind === 'menu' && (
        <MenuScreen
          resources={resources}
          counts={counts}
          analyticsEnabled={analyticsEnabled}
          onOpenAnalytics={() => setRoute({ kind: 'analytics' })}
          onOpen={(name) => setRoute({ kind: 'list', resource: name })}
          onBack={() => backRoute && setRoute(backRoute)}
        />
      )}

      {route.kind === 'file' && (
        <FileScreen field={route.field} value={route.value} onBack={() => setRoute(route.back)} />
      )}

      {navNames.length > 0 && (
        <nav className="sol-nav">
          {navNames.map((name) => (
            <button
              key={name}
              className={`sol-nv${activeResource === name ? ' is-on' : ''}`}
              onClick={() => setRoute({ kind: 'list', resource: name })}
            >
              <Icon name={iconForResource(name, resources[name].title)} size={18} />
              <span>{resources[name].title}</span>
            </button>
          ))}
          {/* Четвёртый пункт — «Ещё», как в эталоне: первые три раздела уже
              есть в ленте выше, дублировать четвёртым тот же список незачем.
              «Ещё» ведёт на «Все разделы», где видны ВСЕ разделы сразу и
              «Аналитика», если она подключена этому боту. */}
          <button
            className={`sol-nv${route.kind === 'menu' || route.kind === 'analytics' ? ' is-on' : ''}`}
            onClick={() => setRoute({ kind: 'menu' })}
          >
            <Icon name="dots" size={18} />
            <span>Ещё</span>
          </button>
        </nav>
      )}
    </>
  )
}
