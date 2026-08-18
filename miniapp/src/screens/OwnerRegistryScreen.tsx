import { useEffect, useMemo, useState } from 'react'
import { listOwnerRegistry, ApiError, type OwnerRegistryItem } from '../lib/factoryApi'
import { Card, CardHeader, CardTitle, ChipRow, Chip, Badge } from '../components/Card'
import { iconForTemplate } from '../lib/botIcons'

// The separate owner-wide registry (multitenancy design item 3):
// FactoryDashboardScreen's "Моя фабрика" already lists every bot to the
// system owner, but its payload/UI never surfaces WHICH customer owns which
// bot (see MEMORY.md's multitenancy design note). This screen exists
// specifically to make that visible — grouped by owner_telegram_id, with a
// filter, and nothing else (no analytics/features/offices — those stay on
// the per-bot detail panel this screen deliberately doesn't link into).

function ownerLabel(ownerId: number | null): string {
  return ownerId == null ? 'без владельца (легаси)' : `владелец #${ownerId}`
}

export function OwnerRegistryScreen({ onBack }: { onBack: () => void }) {
  const [items, setItems] = useState<OwnerRegistryItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [ownerFilter, setOwnerFilter] = useState<number | null | 'all'>('all')

  useEffect(() => {
    listOwnerRegistry()
      .then((data) => setItems(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить реестр'))
  }, [])

  const owners = useMemo(() => {
    const ids = Array.from(new Set((items ?? []).map((b) => b.owner_telegram_id)))
    return ids.sort((a, b) => (a ?? -1) - (b ?? -1))
  }, [items])

  const filtered = (items ?? []).filter((b) => ownerFilter === 'all' || b.owner_telegram_id === ownerFilter)

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

      {filtered.map((bot) => (
        <Card key={bot.id}>
          <CardHeader>
            <CardTitle>
              {iconForTemplate(bot.template)} {bot.display_name || bot.name}
            </CardTitle>
            <Badge tone={bot.status === 'running' ? 'success' : 'neutral'}>{bot.status}</Badge>
          </CardHeader>
          <ChipRow>
            <Chip>{ownerLabel(bot.owner_telegram_id)}</Chip>
            {bot.template && <Chip>{bot.template}</Chip>}
            <Chip>создан: {bot.created_at}</Chip>
          </ChipRow>
        </Card>
      ))}
    </div>
  )
}
