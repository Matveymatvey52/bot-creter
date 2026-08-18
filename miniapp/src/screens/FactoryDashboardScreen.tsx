import { useEffect, useMemo, useState } from 'react'
import {
  listFactoryBots,
  addFactoryFeedback,
  ApiError,
  type FactoryBotItem,
} from '../lib/factoryApi'
import { ChipRow, Chip } from '../components/Card'
import { iconForTemplate } from '../lib/botIcons'
import { BotDetailPanel } from './BotDetailPanel'

const ACTIVE_STATUSES = new Set(['running'])

function isBotActive(status: string): boolean {
  return ACTIVE_STATUSES.has(status)
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
  const [isOwner, setIsOwner] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [templateFilter, setTemplateFilter] = useState<string | null>(null)
  const [featureFilter, setFeatureFilter] = useState<string | null>(null)
  const [selectedBotId, setSelectedBotId] = useState<number | null>(() => {
    const raw = new URLSearchParams(window.location.search).get('bot')
    return raw && /^\d+$/.test(raw) ? Number(raw) : null
  })

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
      (!templateFilter || b.template === templateFilter) &&
      (!featureFilter || b.features.includes(featureFilter)),
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

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Моя фабрика</h1>
      </div>

      {error && <div className="state-message">{error}</div>}
      {!error && items === null && <div className="state-message">Загрузка…</div>}

      {isOwner && templates.length > 0 && (
        <div className="chip-row" style={{ padding: '0 0 8px' }}>
          <button
            className="chip"
            style={!templateFilter ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
            onClick={() => setTemplateFilter(null)}
          >
            Все шаблоны
          </button>
          {templates.map((t) => (
            <button
              key={t}
              className="chip"
              style={t === templateFilter ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
              onClick={() => setTemplateFilter(t === templateFilter ? null : t)}
            >
              {t}
            </button>
          ))}
        </div>
      )}

      {isOwner && features.length > 0 && (
        <div className="chip-row" style={{ padding: '0 0 8px' }}>
          <button
            className="chip"
            style={!featureFilter ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
            onClick={() => setFeatureFilter(null)}
          >
            Все фичи
          </button>
          {features.map((f) => (
            <button
              key={f}
              className="chip"
              style={f === featureFilter ? { background: 'var(--accent)', color: 'var(--accent-text)' } : undefined}
              onClick={() => setFeatureFilter(f === featureFilter ? null : f)}
            >
              {f}
            </button>
          ))}
        </div>
      )}

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
