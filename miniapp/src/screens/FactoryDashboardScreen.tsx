import { useEffect, useMemo, useRef, useState } from 'react'
import {
  listFactoryBots,
  addFactoryFeedback,
  ApiError,
  type FactoryBotItem,
} from '../lib/factoryApi'
import { ChipRow, Chip } from '../components/Card'
import { iconForTemplate } from '../lib/botIcons'
import { BotDetailPanel } from './BotDetailPanel'
import { OwnerRegistryScreen } from './OwnerRegistryScreen'

const ACTIVE_STATUSES = new Set(['running'])

function isBotActive(status: string): boolean {
  return ACTIVE_STATUSES.has(status)
}

// Compact multi-select checkbox dropdown for the top filter bar — replaces
// the old row of individual chip buttons (design change: filters must take
// minimal vertical space and support selecting several values at once).
function MultiSelectDropdown({
  label,
  options,
  selected,
  onChange,
}: {
  label: string
  options: string[]
  selected: Set<string>
  onChange: (next: Set<string>) => void
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  const toggle = (opt: string) => {
    const next = new Set(selected)
    if (next.has(opt)) next.delete(opt)
    else next.add(opt)
    onChange(next)
  }

  return (
    <div className="filter-dropdown" ref={rootRef}>
      <button type="button" className="filter-dropdown-trigger" onClick={() => setOpen(!open)}>
        <span>{label}{selected.size > 0 ? ` (${selected.size})` : ''}</span>
        <span className="filter-dropdown-caret">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="filter-dropdown-panel">
          {options.map((opt) => (
            <label key={opt} className="filter-dropdown-item">
              <input type="checkbox" checked={selected.has(opt)} onChange={() => toggle(opt)} />
              <span>{opt}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

// weekly_count is a generic "records created this week" number (see
// runtime/factory_analytics_api.py's _weekly_count_for_bot) — the label
// stays generic ("записей") rather than guessing per-template wording
// ("заказов"/"записей на приём"/...), since the backend has no per-template
// vocabulary either, only office_hook_config's table name.
function weeklyMetricLabel(bot: FactoryBotItem): string {
  if (!isBotActive(bot.status)) return 'бот приостановлен'
  if (bot.weekly_count == null) return 'нет данных за неделю'
  return 'записей на этой неделе'
}


export function FeedbackForm({ bot, onDone }: { bot: FactoryBotItem; onDone: () => void }) {
  const [rating, setRating] = useState(5)
  const [comment, setComment] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = () => {
    setSubmitting(true)
    setError(null)
    addFactoryFeedback(bot.id, rating, comment || undefined)
      .then(onDone)
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : 'Не удалось сохранить оценку')
        setSubmitting(false)
      })
  }

  return (
    <div className="card" style={{ marginTop: 8 }}>
      <div className="chip-row">
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            className="chip"
            style={n === rating ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
            onClick={() => setRating(n)}
          >
            {n}
          </button>
        ))}
      </div>
      <textarea
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder="Комментарий (необязательно)"
        rows={2}
        style={{ width: '100%', marginTop: 8 }}
      />
      {error && <div className="state-message">{error}</div>}
      <button className="btn-primary" disabled={submitting} onClick={submit} style={{ marginTop: 8 }}>
        Сохранить оценку
      </button>
    </div>
  )
}

export function FactoryDashboardScreen() {
  const [items, setItems] = useState<FactoryBotItem[] | null>(null)
  // is_owner itself has no reader left in this component — the two sections
  // that used to gate on it (template-candidate clusters/candidates) moved
  // to OwnerReportScreen.tsx (multitenancy stage 2), and this screen's own
  // "Реестр" button was removed (see task: header redesign). Kept as a
  // discarded setter rather than dropped entirely since listFactoryBots()
  // still returns is_owner and a future section here may want it again.
  const [, setIsOwner] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [templateFilter, setTemplateFilter] = useState<Set<string>>(new Set())
  const [featureFilter, setFeatureFilter] = useState<Set<string>>(new Set())
  const [selectedBotId, setSelectedBotId] = useState<number | null>(() => {
    const raw = new URLSearchParams(window.location.search).get('bot')
    return raw && /^\d+$/.test(raw) ? Number(raw) : null
  })
  // Separate owner-wide registry (design item 3 — see OwnerRegistryScreen's
  // own header comment): "Моя фабрика" above already lists every bot to the
  // owner, but never says WHICH customer owns which one. A ?view=registry
  // query param toggle (same convention as ?bot=<id> above) keeps it a
  // distinct screen without a full router.
  const [view, setView] = useState<'dashboard' | 'registry'>(() =>
    new URLSearchParams(window.location.search).get('view') === 'registry' ? 'registry' : 'dashboard',
  )

  const closeRegistry = () => {
    setView('dashboard')
    const url = new URL(window.location.href)
    url.searchParams.delete('view')
    window.history.replaceState(null, '', url.toString())
  }

  const reload = () => {
    listFactoryBots()
      .then((data) => {
        setItems(data.items)
        setIsOwner(data.is_owner)
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить данные'))
  }

  useEffect(() => {
    reload()
  }, [])

  const openBot = (botId: number) => {
    setSelectedBotId(botId)
    const url = new URL(window.location.href)
    url.searchParams.set('bot', String(botId))
    window.history.replaceState(null, '', url.toString())
  }

  const closeBotDetail = () => {
    setSelectedBotId(null)
    const url = new URL(window.location.href)
    url.searchParams.delete('bot')
    window.history.replaceState(null, '', url.toString())
  }

  const templates = useMemo(
    () => Array.from(new Set((items ?? []).map((b) => b.template).filter((t): t is string => !!t))).sort(),
    [items],
  )
  const features = useMemo(
    () => Array.from(new Set((items ?? []).flatMap((b) => b.features))).sort(),
    [items],
  )

  const filtered = (items ?? []).filter(
    (b) =>
      (templateFilter.size === 0 || (!!b.template && templateFilter.has(b.template))) &&
      (featureFilter.size === 0 || b.features.some((f) => featureFilter.has(f))),
  )

  if (selectedBotId != null) {
    return (
      <div className="screen">
        <BotDetailPanel
          botId={selectedBotId}
          allBots={items ?? []}
          onBack={closeBotDetail}
          onChanged={reload}
        />
      </div>
    )
  }

  if (view === 'registry') {
    return <OwnerRegistryScreen onBack={closeRegistry} />
  }

  return (
    <div className="screen">
      <div className="factory-dashboard-header">
        <div className="screen-header">
          <h1>Моя фабрика</h1>
        </div>

        {(templates.length > 0 || features.length > 0) && (
          <div className="filter-bar">
            {templates.length > 0 && (
              <MultiSelectDropdown label="Шаблоны" options={templates} selected={templateFilter} onChange={setTemplateFilter} />
            )}
            {features.length > 0 && (
              <MultiSelectDropdown label="Фичи" options={features} selected={featureFilter} onChange={setFeatureFilter} />
            )}
          </div>
        )}
      </div>

      {error && <div className="state-message">{error}</div>}
      {!error && items === null && <div className="state-message">Загрузка…</div>}

      {items !== null && filtered.length === 0 && <div className="state-message">Ничего не найдено</div>}

      {filtered.map((bot) => {
        const active = isBotActive(bot.status)
        return (
          <div className="bot-card" key={bot.id}>
            <button
              className="bot-card-top"
              style={{ background: 'transparent', border: 'none', padding: 0, cursor: 'pointer', textAlign: 'left' }}
              onClick={() => openBot(bot.id)}
            >
              <span className="bot-card-icon">{iconForTemplate(bot.template)}</span>
              <span className="bot-card-id">
                <span className="bot-card-name">{bot.display_name || bot.name}</span>
                {bot.template && <span className="bot-card-template">{bot.template}</span>}
              </span>
              <span className={`status-pill ${active ? 'status-pill-active' : 'status-pill-paused'}`}>
                {active ? (
                  <>
                    <span className="status-dot status-dot-active" />
                    Активен
                  </>
                ) : (
                  '⏸ На паузе'
                )}
              </span>
            </button>

            <button
              className={`bot-card-metric ${active ? '' : 'bot-card-metric-muted'}`}
              style={{ background: 'transparent', border: 'none', padding: 0, cursor: 'pointer', textAlign: 'left' }}
              onClick={() => openBot(bot.id)}
            >
              {active && bot.weekly_count != null && <span className="num">{bot.weekly_count}</span>}
              <span className="label">{weeklyMetricLabel(bot)}</span>
            </button>

            {bot.features.length > 0 && (
              <ChipRow>
                {bot.features.map((f) => (
                  <Chip key={f}>{f}</Chip>
                ))}
              </ChipRow>
            )}

            <div className="bot-card-foot">
              <span>создан: {bot.created_at}</span>
              <button
                className="bot-card-open"
                style={{ background: 'transparent', border: 'none', padding: 0, cursor: 'pointer' }}
                onClick={() => openBot(bot.id)}
              >
                Открыть
                <svg viewBox="0 0 16 16" width="13" height="13" fill="none">
                  <path d="M6 3l5 5-5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </div>
          </div>
        )
      })}

    </div>
  )
}
