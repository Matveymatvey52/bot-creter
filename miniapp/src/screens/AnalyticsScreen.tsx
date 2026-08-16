import { useEffect, useState } from 'react'
import { getAnalytics, ApiError, type AnalyticsData, type AnalyticsPeriod } from '../lib/api'
import { Card, CardHeader, CardTitle, ChipRow, Chip } from '../components/Card'

const PERIOD_LABELS: Record<AnalyticsPeriod, string> = {
  week: 'Неделя',
  month: 'Месяц',
  quarter: 'Квартал',
}

// Deliberately plain CSS bars, no charting library — miniapp/package.json
// has none (react/react-dom only, see docs/SALES_ANALYTICS_DESIGN.md §6's
// "avoid adding a new dependency" guidance), and this dashboard only needs
// two simple shapes (a day-bucketed bar chart, a ranked list), both of
// which render fine as styled <div>s.
function VolumeChart({ points }: { points: AnalyticsData['volume'] }) {
  if (points.length === 0) {
    return <div className="state-message">Недостаточно данных для графика</div>
  }
  const max = Math.max(...points.map((p) => p.count), 1)
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 120, padding: '8px 0' }}>
      {points.map((p) => (
        <div key={p.date} style={{ flex: 1, textAlign: 'center' }} title={`${p.date}: ${p.count}`}>
          <div
            style={{
              background: 'var(--accent)',
              borderRadius: 3,
              height: `${(p.count / max) * 100}px`,
              minHeight: p.count > 0 ? 3 : 0,
            }}
          />
        </div>
      ))}
    </div>
  )
}

function TopCustomersList({ items }: { items: AnalyticsData['top_customers'] }) {
  if (items.length === 0) {
    return <div className="state-message">Нет данных о клиентах</div>
  }
  const max = Math.max(...items.map((c) => c.count), 1)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 0' }}>
      {items.map((c) => (
        <div key={String(c.match_field)} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ width: 90, fontSize: 12, opacity: 0.75, overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {c.match_field}
          </div>
          <div style={{ flex: 1, background: 'var(--surface-2, #eee)', borderRadius: 3 }}>
            <div
              style={{
                background: 'var(--accent)',
                borderRadius: 3,
                width: `${(c.count / max) * 100}%`,
                height: 10,
              }}
            />
          </div>
          <div style={{ width: 24, textAlign: 'right', fontSize: 12 }}>{c.count}</div>
        </div>
      ))}
    </div>
  )
}

function DeltaBadge({ deltaPct }: { deltaPct: number | null }) {
  if (deltaPct === null) {
    return <span style={{ opacity: 0.6 }}>—</span>
  }
  const sign = deltaPct > 0 ? '+' : ''
  const tone = deltaPct > 0 ? 'var(--success, #2a9d5c)' : deltaPct < 0 ? 'var(--danger, #c0392b)' : 'inherit'
  return <span style={{ color: tone }}>{sign}{deltaPct}%</span>
}

export function AnalyticsScreen() {
  const [period, setPeriod] = useState<AnalyticsPeriod>('week')
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [unavailable, setUnavailable] = useState(false)

  useEffect(() => {
    let cancelled = false
    setError(null)
    getAnalytics(period)
      .then((result) => {
        if (cancelled) return
        setData(result)
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
          // 404: feature not applicable to this bot (no usable
          // office_hook_config). 403: authenticated but not this bot's
          // owner (analytics_handler's admin-list check) — the SPA has no
          // separate owner-detection of its own, so a random customer
          // simply never sees this tab render anything, rather than an
          // error message revealing the tab exists. See that handler's
          // docstring for both cases.
          setUnavailable(true)
          return
        }
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить аналитику')
      })
    return () => {
      cancelled = true
    }
  }, [period])

  if (unavailable) {
    return null
  }

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Аналитика</h1>
      </div>

      <ChipRow>
        {(Object.keys(PERIOD_LABELS) as AnalyticsPeriod[]).map((p) => (
          <button
            key={p}
            className="chip"
            style={p === period ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
            onClick={() => {
              setData(null)
              setPeriod(p)
            }}
          >
            {PERIOD_LABELS[p]}
          </button>
        ))}
      </ChipRow>

      {error && <div className="state-message">{error}</div>}
      {!error && data === null && <div className="state-message">Загрузка…</div>}

      {data && (
        <>
          <Card>
            <ChipRow>
              <Chip>всего записей: {data.total}</Chip>
              <Chip>
                изменение за период: <DeltaBadge deltaPct={data.delta_pct} />
              </Chip>
              {data.new_vs_returning && (
                <>
                  <Chip>новые: {data.new_vs_returning.new}</Chip>
                  <Chip>повторные: {data.new_vs_returning.returning}</Chip>
                </>
              )}
            </ChipRow>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Записи по дням</CardTitle>
            </CardHeader>
            <VolumeChart points={data.volume} />
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Постоянные клиенты</CardTitle>
            </CardHeader>
            <TopCustomersList items={data.top_customers} />
          </Card>
        </>
      )}
    </div>
  )
}
