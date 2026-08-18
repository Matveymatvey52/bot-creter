import { useEffect, useMemo, useState } from 'react'
import { listOwnerRegistry, ApiError, type OwnerRegistryItem } from '../lib/factoryApi'
import { Card, CardHeader, CardTitle, ChipRow, Chip, Badge } from '../components/Card'
import { iconForTemplate } from '../lib/botIcons'
import { BotDetailPanel } from './BotDetailPanel'

// The separate owner-wide registry (multitenancy design item 3):
// FactoryDashboardScreen's "Моя фабрика" already lists every bot to the
// system owner, but its payload/UI never surfaces WHICH customer owns which
// bot (see MEMORY.md's multitenancy design note). This screen exists
// specifically to make that visible — grouped by owner_telegram_id.
//
// Upgraded from a minimal "who owns what" list into full owner-facing
// product analytics (design ask: "не упрощай"): summary stat tiles, a
// full table per bot (features/edits/rating/weekly activity), and a
// clickable row -> bot detail. It reuses BotDetailPanel directly rather
// than building a parallel read-only detail view: BotDetailPanel's `allBots`
// prop only needs FactoryBotItem's fields for cross-bot office linking and
// its own bot lookup, and OwnerRegistryItem is now a structural superset of
// FactoryBotItem (same fields plus owner_telegram_id), so passing the
// registry's own `items` array as `allBots` just works — no separate
// read-only card was needed, and this screen is owner-only exactly like the
// dashboard BotDetailPanel already assumes.

function ownerLabel(ownerId: number | null): string {
  return ownerId == null ? 'без владельца (легаси)' : `владелец #${ownerId}`
}

function isBotActive(status: string): boolean {
  return status === 'running'
}

function StatTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="registry-stat-tile">
      <span className="registry-stat-value">{value}</span>
      <span className="registry-stat-label">{label}</span>
    </div>
  )
}

export function OwnerRegistryScreen({ onBack }: { onBack: () => void }) {
  const [items, setItems] = useState<OwnerRegistryItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ownerFilter, setOwnerFilter] = useState<number | null | 'all'>('all')
  const [selectedBotId, setSelectedBotId] = useState<number | null>(null)

  const reload = () => {
    listOwnerRegistry()
      .then((data) => setItems(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить реестр'))
  }

  useEffect(() => {
    reload()
  }, [])

  const owners = useMemo(() => {
    const ids = Array.from(new Set((items ?? []).map((b) => b.owner_telegram_id)))
    return ids.sort((a, b) => (a ?? -1) - (b ?? -1))
  }, [items])

  // Client-computed summary stats (see owner_registry_handler's docstring —
  // deliberately not a backend aggregation endpoint): total bots, per-template
  // breakdown ("no template" grouped as from-scratch), distinct owners,
  // active/paused split, average rating, total edits, feature adoption.
  const stats = useMemo(() => {
    const list = items ?? []
    const total = list.length
    const byTemplate = new Map<string, number>()
    let fromScratch = 0
    for (const b of list) {
      if (b.template) byTemplate.set(b.template, (byTemplate.get(b.template) ?? 0) + 1)
      else fromScratch += 1
    }
    const distinctOwners = new Set(list.map((b) => b.owner_telegram_id).filter((id) => id != null)).size
    const active = list.filter((b) => isBotActive(b.status)).length
    const paused = total - active
    const rated = list.filter((b) => b.avg_rating != null)
    const avgRating = rated.length > 0 ? rated.reduce((sum, b) => sum + (b.avg_rating ?? 0), 0) / rated.length : null
    const totalEdits = list.reduce((sum, b) => sum + b.edits_count, 0)
    const featureAdoption = new Map<string, number>()
    for (const b of list) {
      for (const f of b.features) {
        featureAdoption.set(f, (featureAdoption.get(f) ?? 0) + 1)
      }
    }
    const topTemplates = Array.from(byTemplate.entries()).sort((a, b) => b[1] - a[1])
    const topFeatures = Array.from(featureAdoption.entries()).sort((a, b) => b[1] - a[1])
    return { total, fromScratch, distinctOwners, active, paused, avgRating, totalEdits, topTemplates, topFeatures }
  }, [items])

  const filtered = (items ?? []).filter((b) => ownerFilter === 'all' || b.owner_telegram_id === ownerFilter)

  if (selectedBotId != null) {
    return (
      <div className="screen">
        <BotDetailPanel
          botId={selectedBotId}
          allBots={items ?? []}
          onBack={() => setSelectedBotId(null)}
          onChanged={reload}
        />
      </div>
    )
  }

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Реестр ботов по владельцам</h1>
        <button className="btn-secondary" onClick={onBack}>
          ← Назад
        </button>
      </div>

      {error && <div className="state-message">{error}</div>}
      {!error && items === null && <div className="state-message">Загрузка…</div>}
      {items !== null && items.length === 0 && <div className="state-message">Ботов пока нет.</div>}

      {items !== null && items.length > 0 && (
        <div className="registry-stats-grid">
          <StatTile label="всего ботов" value={stats.total} />
          <StatTile label="активных" value={stats.active} />
          <StatTile label="на паузе" value={stats.paused} />
          <StatTile label="клиентов-владельцев" value={stats.distinctOwners} />
          <StatTile label="с нуля (без шаблона)" value={stats.fromScratch} />
          <StatTile label="суммарно правок" value={stats.totalEdits} />
          <StatTile label="средняя оценка" value={stats.avgRating != null ? stats.avgRating.toFixed(1) : '—'} />
        </div>
      )}

      {items !== null && items.length > 0 && stats.topTemplates.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Боты по шаблонам</CardTitle>
          </CardHeader>
          <ChipRow>
            {stats.topTemplates.map(([tpl, count]) => (
              <Chip key={tpl}>{tpl}: {count}</Chip>
            ))}
            {stats.fromScratch > 0 && <Chip>с нуля: {stats.fromScratch}</Chip>}
          </ChipRow>
        </Card>
      )}

      {items !== null && items.length > 0 && stats.topFeatures.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Подключённые фичи</CardTitle>
          </CardHeader>
          <ChipRow>
            {stats.topFeatures.map(([f, count]) => (
              <Chip key={f}>{f}: {count}</Chip>
            ))}
          </ChipRow>
        </Card>
      )}

      {owners.length > 1 && (
        <ChipRow>
          <button
            className="chip"
            style={ownerFilter === 'all' ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
            onClick={() => setOwnerFilter('all')}
          >
            все ({items?.length ?? 0})
          </button>
          {owners.map((ownerId) => (
            <button
              key={ownerId ?? 'none'}
              className="chip"
              style={ownerFilter === ownerId ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
              onClick={() => setOwnerFilter(ownerId)}
            >
              {ownerLabel(ownerId)}
            </button>
          ))}
        </ChipRow>
      )}

      {filtered.length > 0 && (
        <div className="registry-table-wrap">
          <table className="registry-table">
            <thead>
              <tr>
                <th>Бот</th>
                <th>Владелец</th>
                <th>Шаблон</th>
                <th>Статус</th>
                <th>Создан</th>
                <th>Фичи</th>
                <th>Правок</th>
                <th>Активность (нед.)</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((bot) => (
                <tr key={bot.id} className="registry-table-row" onClick={() => setSelectedBotId(bot.id)}>
                  <td>
                    <span className="registry-table-bot-name">
                      {iconForTemplate(bot.template)} {bot.display_name || bot.name}
                    </span>
                  </td>
                  <td>{ownerLabel(bot.owner_telegram_id)}</td>
                  <td>{bot.template || 'с нуля'}</td>
                  <td>
                    <Badge tone={isBotActive(bot.status) ? 'success' : 'neutral'}>{bot.status}</Badge>
                  </td>
                  <td>{bot.created_at}</td>
                  <td>{bot.features.length > 0 ? bot.features.join(', ') : '—'}</td>
                  <td>{bot.edits_count}</td>
                  <td>{bot.weekly_count != null ? bot.weekly_count : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
