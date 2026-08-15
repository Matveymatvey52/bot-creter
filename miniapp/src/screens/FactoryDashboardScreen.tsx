import { useEffect, useMemo, useState } from 'react'
import { listFactoryBots, addFactoryFeedback, ApiError, type FactoryBotItem } from '../lib/factoryApi'
import { Card, CardHeader, CardTitle, ChipRow, Chip, Badge } from '../components/Card'

const ACTIVE_STATUSES = new Set(['running'])

function statusTone(status: string): 'success' | 'neutral' {
  return ACTIVE_STATUSES.has(status) ? 'success' : 'neutral'
}

function FeedbackForm({ bot, onDone }: { bot: FactoryBotItem; onDone: () => void }) {
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
  const [error, setError] = useState<string | null>(null)
  const [templateFilter, setTemplateFilter] = useState<string | null>(null)
  const [featureFilter, setFeatureFilter] = useState<string | null>(null)
  const [feedbackTargetId, setFeedbackTargetId] = useState<number | null>(null)

  useEffect(() => {
    listFactoryBots()
      .then((data) => setItems(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить данные'))
  }, [])

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

  const refreshAfterFeedback = () => {
    setFeedbackTargetId(null)
    listFactoryBots().then((data) => setItems(data.items))
  }

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Аналитика фабрики</h1>
      </div>

      {error && <div className="state-message">{error}</div>}
      {!error && items === null && <div className="state-message">Загрузка…</div>}

      {templates.length > 0 && (
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

      {features.length > 0 && (
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

      {filtered.map((bot) => (
        <Card key={bot.id}>
          <CardHeader>
            <CardTitle>{bot.display_name || bot.name}</CardTitle>
            <Badge tone={statusTone(bot.status)}>{bot.status}</Badge>
          </CardHeader>
          <ChipRow>
            {bot.template && <Chip>шаблон: {bot.template}</Chip>}
            <Chip>создан: {bot.created_at}</Chip>
            <Chip>правок: {bot.edits_count}</Chip>
            {bot.avg_rating != null && (
              <Chip>рейтинг: {bot.avg_rating.toFixed(1)} ({bot.feedback_count})</Chip>
            )}
            {bot.archived_at && <Chip>архив: {bot.archived_at}</Chip>}
          </ChipRow>
          {bot.features.length > 0 && (
            <ChipRow>
              {bot.features.map((f) => (
                <Chip key={f}>{f}</Chip>
              ))}
            </ChipRow>
          )}
          {feedbackTargetId === bot.id ? (
            <FeedbackForm bot={bot} onDone={refreshAfterFeedback} />
          ) : (
            <button className="btn-primary" style={{ marginTop: 8 }} onClick={() => setFeedbackTargetId(bot.id)}>
              Оценить
            </button>
          )}
        </Card>
      ))}
    </div>
  )
}
