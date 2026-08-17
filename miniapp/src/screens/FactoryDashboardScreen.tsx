import { useEffect, useMemo, useState } from 'react'
import {
  listFactoryBots,
  addFactoryFeedback,
  listTemplateCandidates,
  listTemplateCandidateClusters,
  ApiError,
  type FactoryBotItem,
  type TemplateCandidateItem,
  type TemplateCandidateClusterItem,
} from '../lib/factoryApi'
import { Card, CardHeader, CardTitle, ChipRow, Chip, Badge } from '../components/Card'

const FALLBACK_REASON_LABELS: Record<string, string> = {
  no_template_match: 'нет подходящего шаблона',
  customize_failed: 'шаблон подошёл, но кастомизация не удалась',
  synthesis_failed: 'два шаблона подошли, но синтез не удался',
}

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

// docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md §4 — server-side clusters from
// runtime/template_candidate_clustering.py's daily background pass, largest
// first. Highlight threshold (count >= 3) is pure display, not a DB
// invariant — the owner-approved starting point, tunable here without a
// migration.
const CLUSTER_HIGHLIGHT_THRESHOLD = 3

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

      <TemplateCandidateClustersSection />
      <TemplateCandidatesSection />
    </div>
  )
}
