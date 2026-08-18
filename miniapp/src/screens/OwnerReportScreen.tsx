import { useEffect, useMemo, useState } from 'react'
import {
  listOwnerReportBots,
  listOwnerReportActivity,
  listTemplateCandidates,
  listTemplateCandidateClusters,
  ApiError,
  type OwnerReportBotItem,
  type OwnerReportActivityItem,
  type TemplateCandidateItem,
  type TemplateCandidateClusterItem,
} from '../lib/ownerReportApi'
import { Card, CardHeader, CardTitle, ChipRow, Chip, Badge } from '../components/Card'

// Internal analytics tool for the product owner, not a customer-facing
// product (see the approved Stage 2 design) — functional and readable, no
// extra visual investment beyond what's already in ui.css.

// Fallback-reason labels + cluster highlight threshold, moved here from
// FactoryDashboardScreen.tsx alongside TemplateCandidatesSection/
// TemplateCandidateClustersSection below (owner instruction, 2026-08-18):
// these sections never showed per-owner data, they belong in the cross-owner
// report, not "Моя фабрика".
const FALLBACK_REASON_LABELS: Record<string, string> = {
  no_template_match: 'нет подходящего шаблона',
  customize_failed: 'шаблон подошёл, но кастомизация не удалась',
  synthesis_failed: 'два шаблона подошли, но синтез не удался',
}

const CLUSTER_HIGHLIGHT_THRESHOLD = 3

type Tab = 'bots' | 'activity' | 'patterns'

type SortKey = 'created_at' | 'name' | 'owner_telegram_id' | 'template' | 'status' | 'last_activity_at' | 'feedback_count' | 'edits_count'

const SOURCE_LABELS: Record<OwnerReportActivityItem['source'], string> = {
  office_event: 'офис-событие',
  feedback: 'отзыв',
  payment: 'платёж',
}

function formatBytes(bytes: number | null): string {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`
}

export function OwnerReportScreen() {
  const [tab, setTab] = useState<Tab>('bots')
  const [bots, setBots] = useState<OwnerReportBotItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    let cancelled = false
    listOwnerReportBots()
      .then((data) => {
        if (cancelled) return
        setBots(data.items)
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 403) {
          setForbidden(true)
          return
        }
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить отчёт')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (forbidden) {
    return <div className="state-message">Доступ только для владельца фабрики</div>
  }
  if (error) {
    return <div className="state-message">{error}</div>
  }

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Отчёт владельца</h1>
      </div>

      <ChipRow>
        <button
          className="chip"
          style={tab === 'bots' ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
          onClick={() => setTab('bots')}
        >
          Реестр ботов
        </button>
        <button
          className="chip"
          style={tab === 'activity' ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
          onClick={() => setTab('activity')}
        >
          Лента активности
        </button>
        <button
          className="chip"
          style={tab === 'patterns' ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
          onClick={() => setTab('patterns')}
        >
          Незакрытые паттерны
        </button>
      </ChipRow>

      {tab === 'bots' && (bots === null ? <div className="state-message">Загрузка…</div> : <BotRegistryTable bots={bots} />)}
      {tab === 'activity' && <ActivityFeed bots={bots ?? []} />}
      {tab === 'patterns' && (
        <>
          <TemplateCandidateClustersSection />
          <TemplateCandidatesSection />
        </>
      )}
    </div>
  )
}

// Moved from FactoryDashboardScreen.tsx (owner instruction, 2026-08-18) — see
// module-level comment above.
function TemplateCandidateClustersSection() {
  const [clusters, setClusters] = useState<TemplateCandidateClusterItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listTemplateCandidateClusters()
      .then((data) => setClusters(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить паттерны'))
  }, [])

  return (
    <div className="screen-section">
      <h2 style={{ margin: '16px 0 8px' }}>Топ незакрытых паттернов</h2>
      {error && <div className="state-message">{error}</div>}
      {!error && clusters === null && <div className="state-message">Загрузка…</div>}
      {clusters !== null && clusters.length === 0 && (
        <div className="state-message">Пока нет обработанных кластеров — ждём следующий проход анализа.</div>
      )}
      {clusters?.map((c) => (
        <Card key={c.id}>
          <CardHeader>
            <CardTitle>{c.label}</CardTitle>
            <Badge tone={c.count >= CLUSTER_HIGHLIGHT_THRESHOLD ? 'success' : 'neutral'}>{c.count}</Badge>
          </CardHeader>
          {c.description && <div style={{ marginTop: 4, opacity: 0.8 }}>{c.description}</div>}
          <ChipRow>
            <Chip>впервые: {c.first_seen}</Chip>
            <Chip>последний раз: {c.last_seen}</Chip>
          </ChipRow>
          {c.examples.map((summary, i) => (
            <div key={i} style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border, #333)' }}>
              {summary}
            </div>
          ))}
        </Card>
      ))}
    </div>
  )
}

function TemplateCandidatesSection() {
  const [candidates, setCandidates] = useState<TemplateCandidateItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listTemplateCandidates()
      .then((data) => setCandidates(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить кандидатов'))
  }, [])

  // Lightweight signal, not real clustering (see docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md
  // §3's MVP decision) — group by bot_type so recurring requests for the
  // same kind of bot are visually obvious without inventing NLP clustering.
  const groups = useMemo(() => {
    if (!candidates) return []
    const byType = new Map<string, TemplateCandidateItem[]>()
    for (const c of candidates) {
      const key = c.bot_type || 'без категории'
      if (!byType.has(key)) byType.set(key, [])
      byType.get(key)!.push(c)
    }
    return Array.from(byType.entries()).sort((a, b) => b[1].length - a[1].length)
  }, [candidates])

  return (
    <div className="screen-section">
      <h2 style={{ margin: '16px 0 8px' }}>Кандидаты на новый шаблон</h2>
      {error && <div className="state-message">{error}</div>}
      {!error && candidates === null && <div className="state-message">Загрузка…</div>}
      {candidates !== null && candidates.length === 0 && (
        <div className="state-message">Пока нет ботов, для которых не нашёлся подходящий шаблон.</div>
      )}
      {groups.map(([botType, group]) => (
        <Card key={botType}>
          <CardHeader>
            <CardTitle>{botType}</CardTitle>
            <Badge tone="neutral">{group.length}</Badge>
          </CardHeader>
          {group.map((c) => (
            <div key={c.id} style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border, #333)' }}>
              <div>{c.summary}</div>
              <ChipRow>
                <Chip>{FALLBACK_REASON_LABELS[c.fallback_reason] || c.fallback_reason}</Chip>
                <Chip>{c.created_at}</Chip>
                {c.bot_name && <Chip>бот: {c.bot_name}</Chip>}
                {c.selected_templates.length > 0 && (
                  <Chip>рассматривались: {c.selected_templates.join(', ')}</Chip>
                )}
              </ChipRow>
            </div>
          ))}
        </Card>
      ))}
    </div>
  )
}

function BotRegistryTable({ bots }: { bots: OwnerReportBotItem[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('created_at')
  const [sortDir, setSortDir] = useState<1 | -1>(-1)
  const [ownerFilter, setOwnerFilter] = useState<string>('')
  const [templateFilter, setTemplateFilter] = useState<string>('')
  const [statusFilter, setStatusFilter] = useState<string>('')

  const owners = useMemo(
    () => Array.from(new Set(bots.map((b) => b.owner_display).filter((v): v is string => v != null))).sort(),
    [bots],
  )
  const templates = useMemo(
    () => Array.from(new Set(bots.map((b) => b.template).filter((v): v is string => v != null))).sort(),
    [bots],
  )
  const statuses = useMemo(() => Array.from(new Set(bots.map((b) => b.status))).sort(), [bots])

  const filtered = useMemo(() => {
    return bots.filter((b) => {
      if (ownerFilter && b.owner_display !== ownerFilter) return false
      if (templateFilter && b.template !== templateFilter) return false
      if (statusFilter && b.status !== statusFilter) return false
      return true
    })
  }, [bots, ownerFilter, templateFilter, statusFilter])

  const sorted = useMemo(() => {
    const copy = [...filtered]
    copy.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      if (av < bv) return -1 * sortDir
      if (av > bv) return 1 * sortDir
      return 0
    })
    return copy
  }, [filtered, sortKey, sortDir])

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 1 ? -1 : 1) as 1 | -1)
    } else {
      setSortKey(key)
      setSortDir(-1)
    }
  }

  const headerCell = (key: SortKey, label: string) => (
    <th onClick={() => toggleSort(key)} style={{ cursor: 'pointer', whiteSpace: 'nowrap' }}>
      {label}
      {sortKey === key ? (sortDir === 1 ? ' ▲' : ' ▼') : ''}
    </th>
  )

  return (
    <>
      <ChipRow>
        <select value={ownerFilter} onChange={(e) => setOwnerFilter(e.target.value)}>
          <option value="">Все владельцы</option>
          {owners.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        <select value={templateFilter} onChange={(e) => setTemplateFilter(e.target.value)}>
          <option value="">Все шаблоны</option>
          {templates.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">Все статусы</option>
          {statuses.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <Chip>{sorted.length} из {bots.length}</Chip>
      </ChipRow>

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr>
              {headerCell('name', 'Бот')}
              {headerCell('owner_telegram_id', 'Владелец')}
              {headerCell('template', 'Шаблон')}
              {headerCell('status', 'Статус')}
              <th>Фичи</th>
              {headerCell('edits_count', 'Правок')}
              {headerCell('feedback_count', 'Отзывы')}
              <th>Оплата</th>
              {headerCell('created_at', 'Создан')}
              {headerCell('last_activity_at', 'Последняя активность')}
              <th>Объём БД</th>
              <th>Промпт создания</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((b) => (
              <tr key={b.id} style={{ borderTop: '1px solid var(--border, #e0e0e0)' }}>
                <td>
                  {b.name}
                  {b.username ? ` (@${b.username})` : ''}
                </td>
                <td>{b.owner_display ?? '—'}</td>
                <td>{b.template ?? '—'}</td>
                <td>
                  <Badge tone={b.status === 'running' ? 'success' : 'neutral'}>{b.status}</Badge>
                </td>
                <td>{b.features.length ? b.features.join(', ') : '—'}</td>
                <td>{b.edits_count}</td>
                <td>
                  {b.feedback_count}
                  {b.avg_rating != null ? ` (${b.avg_rating.toFixed(1)}★)` : ''}
                </td>
                <td>{b.payments_connected ? '✅' : '—'}</td>
                <td>{b.created_at}</td>
                <td>{b.last_activity_at ?? '—'}</td>
                <td>{formatBytes(b.approx_data_volume_bytes)}</td>
                <td style={{ maxWidth: 240, whiteSpace: 'normal', opacity: 0.8 }}>{b.creation_prompt ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function ActivityFeed({ bots }: { bots: OwnerReportBotItem[] }) {
  const [items, setItems] = useState<OwnerReportActivityItem[] | null>(null)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [ownerFilter, setOwnerFilter] = useState<string>('')
  const [botFilter, setBotFilter] = useState<string>('')
  const [offset, setOffset] = useState(0)
  const limit = 50

  const owners = useMemo(
    () => Array.from(new Set(bots.map((b) => b.owner_display).filter((v): v is string => v != null))).sort(),
    [bots],
  )

  useEffect(() => {
    let cancelled = false
    setItems(null)
    const ownerId = ownerFilter ? Number(ownerFilter) : undefined
    const botId = botFilter ? Number(botFilter) : undefined
    listOwnerReportActivity({ ownerId, botId, limit, offset })
      .then((data) => {
        if (cancelled) return
        setItems(data.items)
        setTotal(data.total)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить ленту активности')
      })
    return () => {
      cancelled = true
    }
  }, [ownerFilter, botFilter, offset])

  if (error) {
    return <div className="state-message">{error}</div>
  }

  return (
    <>
      <ChipRow>
        <select
          value={ownerFilter}
          onChange={(e) => {
            setOwnerFilter(e.target.value)
            setOffset(0)
          }}
        >
          <option value="">Все владельцы</option>
          {owners.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        <select
          value={botFilter}
          onChange={(e) => {
            setBotFilter(e.target.value)
            setOffset(0)
          }}
        >
          <option value="">Все боты</option>
          {bots.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}
            </option>
          ))}
        </select>
      </ChipRow>

      {items === null && <div className="state-message">Загрузка…</div>}

      {items && items.length === 0 && <div className="state-message">Активности не найдено</div>}

      {items &&
        items.map((entry, idx) => (
          <Card key={`${entry.source}-${entry.bot_id}-${idx}-${entry.created_at}`}>
            <CardHeader>
              <CardTitle>
                {SOURCE_LABELS[entry.source]} · {entry.bot_name}
              </CardTitle>
            </CardHeader>
            <ChipRow>
              <Chip>{entry.event_type}</Chip>
              <Chip>владелец: {entry.owner_telegram_id ?? '—'}</Chip>
              {entry.telegram_user_id != null && <Chip>пользователь: {entry.telegram_user_id}</Chip>}
              <Chip>{entry.created_at}</Chip>
            </ChipRow>
            {entry.detail && <div style={{ marginTop: 6, fontSize: 13 }}>{entry.detail}</div>}
          </Card>
        ))}

      {items && total > limit && (
        <ChipRow>
          <button className="chip" disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - limit))}>
            ← назад
          </button>
          <Chip>
            {offset + 1}–{Math.min(offset + limit, total)} из {total}
          </Chip>
          <button className="chip" disabled={offset + limit >= total} onClick={() => setOffset((o) => o + limit)}>
            вперёд →
          </button>
        </ChipRow>
      )}
    </>
  )
}
