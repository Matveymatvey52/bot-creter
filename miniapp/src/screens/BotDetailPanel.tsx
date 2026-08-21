import { useEffect, useState } from 'react'
import {
  getBotDetail,
  startBot,
  stopBot,
  restartBot,
  deleteBot,
  getBotLogs,
  recreateBot,
  autofixBot,
  previewFixBug,
  applyFixBug,
  type FixBugPreview,
  getBotSchema,
  listBotResource,
  updateBotResource,
  deleteBotResource,
  type FactorySchemaResource,
  type FactoryResourceItem,
  disableFeature,
  configureFeature,
  cancelFeatureConfigure,
  addOffice,
  removeOffice,
  listOfficeEventTypes,
  getShowcaseGroupStatus,
  addAdmin,
  removeAdmin,
  getBotActivity,
  ApiError,
  type BotDetail,
  type FeatureStatusItem,
  type FactoryBotItem,
  type BotActivityItem,
} from '../lib/factoryApi'
import { iconForTemplate } from '../lib/botIcons'
import { FeedbackForm } from './FactoryDashboardScreen'

type Tab = 'overview' | 'features' | 'offices' | 'admins' | 'data' | 'maintenance'

const TABS: { key: Tab; label: string }[] = [
  { key: 'overview', label: 'Обзор' },
  { key: 'features', label: 'Фичи' },
  { key: 'offices', label: 'Офисы' },
  { key: 'admins', label: 'Админы' },
  { key: 'data', label: 'Данные' },
  { key: 'maintenance', label: 'Обслуживание' },
]

// Static per-feature "что это даст / что нужно будет сделать" guide shown
// before the tumbler's configure step opens — see the approved Variant D
// design ("Шаг 1 — Гайд"). Deliberately NOT Claude-generated: this is fixed,
// predictable copy, not a conversation. payments/office_events are excluded
// (see factoryApi's no_free_text — they never reach this guide's "Продолжить
// → open textarea" branch, payments instead points at the Telegram wizard).
const FEATURE_GUIDES: Record<string, { what: string; steps: string[] }> = {
  sheets: {
    what: 'Каждая новая запись клиента будет дублироваться в вашу Google Таблицу автоматически.',
    steps: ['Написать, что именно записывать', 'Подключить таблицу по ссылке (в Telegram-боте)'],
  },
  notifications: {
    what: 'Вы сможете рассылать сообщения всем, кто хоть раз писал боту.',
    steps: ['Описать, что и как рассылать'],
  },
  reminders: {
    what: 'Бот будет сам напоминать клиентам о предстоящей записи.',
    steps: ['Описать, о чём и когда напоминать'],
  },
  sales_analytics: {
    what: 'В мини-приложении бота появится раздел с бизнес-метриками.',
    steps: ['Описать, какие метрики важны (или продолжить со стандартными)'],
  },
  voice_intake: {
    what: 'Голосовые сообщения будут автоматически превращаться в записи.',
    steps: ['Описать, какие голосовые сообщения и что из них извлекать'],
  },
  sellable_items: {
    what: 'В боте появится каталог товаров/услуг с оплатой.',
    steps: ['Описать, что продаём (или начать с пустого каталога)'],
  },
  cashflow_ledger: {
    what: 'Бот будет вести учёт денег: приход и расход.',
    steps: ['Описать, как группировать записи'],
  },
}

const FEATURE_QUESTION: Record<string, string> = {
  sheets: 'Что записывать в таблицу?',
  notifications: 'Что и как рассылать подписчикам?',
  reminders: 'О чём и когда напоминать клиентам?',
  sales_analytics: 'Какие метрики важны?',
  voice_intake: 'Какие голосовые сообщения превращать в записи?',
  sellable_items: 'Что продаём? Опиши позиции или просто подтверди каталог.',
  cashflow_ledger: 'Как вести учёт — по чему группировать?',
}

const FEATURE_LABELS: Record<string, string> = {
  payments: 'Платежи',
  sheets: 'Google Таблицы',
  notifications: 'Рассылки',
  office_events: 'Обмен между ботами',
  reminders: 'Напоминания',
  sales_analytics: 'Аналитика продаж',
  voice_intake: 'Голосовой ввод',
  sellable_items: 'Каталог товаров',
  cashflow_ledger: 'Учёт денег (ДДС)',
}

function featureLabel(name: string): string {
  return FEATURE_LABELS[name] || name
}

export function BotDetailPanel({
  botId,
  allBots,
  onBack,
  onChanged,
}: {
  botId: number
  allBots: FactoryBotItem[]
  onBack: () => void
  onChanged: () => void
}) {
  const [detail, setDetail] = useState<BotDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = () => {
    getBotDetail(botId)
      .then(setDetail)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить бота'))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botId])

  const runAction = async (fn: () => Promise<unknown>, opts?: { skipReload?: boolean }) => {
    setBusy(true)
    setActionError(null)
    try {
      await fn()
      if (!opts?.skipReload) {
        load()
      }
      onChanged()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Не удалось выполнить действие')
    } finally {
      setBusy(false)
    }
  }

  if (error) {
    return (
      <div>
        <button className="detail-back-link" onClick={onBack}>
          <BackIcon /> Мои боты
        </button>
        <div className="state-message">{error}</div>
      </div>
    )
  }
  if (!detail) {
    return <div className="state-message">Загрузка…</div>
  }

  const active = detail.running

  return (
    <div>
      <div className="detail-header">
        <div className="detail-back-row">
          <button className="detail-back-link" onClick={onBack}>
            <BackIcon /> Мои боты
          </button>
          <button className="detail-close-x" onClick={onBack}>
            ×
          </button>
        </div>
        <div className="detail-top">
          <span className="bot-card-icon">{iconForTemplate(detail.template)}</span>
          <span className="bot-card-id">
            <span className="bot-card-name">{detail.display_name || detail.name}</span>
            <span className="bot-card-template">
              ⚙ {detail.template || 'from-scratch'}
              {detail.username ? ` · @${detail.username}` : ''}
            </span>
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
        </div>
        <div className="detail-quick-actions">
          {active ? (
            <button className="qa-btn" disabled={busy} onClick={() => runAction(() => stopBot(botId))}>
              <span className="ic">⏸</span>Пауза
            </button>
          ) : (
            <button className="qa-btn" disabled={busy} onClick={() => runAction(() => startBot(botId))}>
              <span className="ic">▶️</span>Запуск
            </button>
          )}
          <button className="qa-btn" disabled={busy} onClick={() => runAction(() => restartBot(botId))}>
            <span className="ic">🔁</span>Рестарт
          </button>
          <button className="qa-btn" disabled={busy} onClick={() => setTab('overview')}>
            <span className="ic">📋</span>Логи
          </button>
          <button
            className="qa-btn danger"
            disabled={busy}
            onClick={() => {
              if (window.confirm(`Удалить бота «${detail.name}»? Это необратимо.`)) {
                runAction(() => deleteBot(botId), { skipReload: true }).then(onBack)
              }
            }}
          >
            <span className="ic">🗑</span>Удалить
          </button>
        </div>
      </div>

      <div className="detail-tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`detail-tab ${tab === t.key ? 'active' : ''}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="detail-panel-body">
        {actionError && <div className="state-message">{actionError}</div>}
        {tab === 'overview' && <OverviewTab botId={botId} detail={detail} />}
        {tab === 'features' && (
          <FeaturesTab botId={botId} features={detail.features} template={detail.template} onChanged={load} />
        )}
        {tab === 'offices' && (
          <OfficesTab botId={botId} offices={detail.offices} allBots={allBots} onChanged={load} />
        )}
        {tab === 'admins' && <AdminsTab botId={botId} admins={detail.admins} onChanged={load} />}
        {tab === 'data' && <DataTab botId={botId} />}
        {tab === 'maintenance' && (
          <MaintenanceTab
            botId={botId}
            bot={allBots.find((b) => b.id === botId) ?? null}
            busy={busy}
            setBusy={setBusy}
            setActionError={setActionError}
            onChanged={load}
          />
        )}
      </div>
    </div>
  )
}

function BackIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none">
      <path d="M6 3l5 5-5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

const ACTIVITY_SOURCE_LABELS: Record<BotActivityItem['source'], string> = {
  feedback: 'отзыв',
  office_event: 'офис-событие',
  payment: 'платёж',
}

function OverviewTab({ botId, detail }: { botId: number; detail: BotDetail }) {
  const [logs, setLogs] = useState<string | null>(null)
  const [activity, setActivity] = useState<BotActivityItem[] | null>(null)
  const enabledCount = detail.features.filter((f) => f.state === 'on').length

  useEffect(() => {
    getBotLogs(botId)
      .then((r) => setLogs(r.logs))
      .catch(() => setLogs(null))
    getBotActivity(botId)
      .then((r) => setActivity(r.items))
      .catch(() => setActivity(null))
  }, [botId])

  return (
    <div>
      <div className="mini-grid">
        <div className="mini-stat">
          <div className="n">{enabledCount}</div>
          <div className="l">активные фичи</div>
        </div>
        <div className="mini-stat">
          <div className="n">{detail.admins.length}</div>
          <div className="l">админов</div>
        </div>
        <div className="mini-stat">
          <div className="n">{detail.offices.length}</div>
          <div className="l">связей с офисами</div>
        </div>
        <div className="mini-stat">
          <div className="n">{detail.created_at}</div>
          <div className="l">создан</div>
        </div>
      </div>

      {detail.creation_prompt && (
        <>
          <div className="feature-name" style={{ marginBottom: 6 }}>
            Промпт создания
          </div>
          <div className="feature-desc" style={{ marginBottom: 16 }}>
            {detail.creation_prompt}
          </div>
        </>
      )}

      <div className="feature-name" style={{ marginBottom: 6 }}>
        Активность
      </div>
      {activity && activity.length > 0 ? (
        <div style={{ marginBottom: 16 }}>
          {activity.slice(0, 20).map((item, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                gap: 8,
                padding: '6px 0',
                borderBottom: '1px solid var(--border, #333)',
                fontSize: 12.5,
              }}
            >
              <span style={{ color: 'var(--text-secondary)' }}>
                {ACTIVITY_SOURCE_LABELS[item.source]} · {item.event_type}
                {item.detail ? ` — ${item.detail}` : ''}
              </span>
              <span style={{ color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>{item.created_at}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="feature-desc" style={{ marginBottom: 16 }}>
          {activity === null ? 'Не удалось загрузить активность.' : 'Активности пока нет.'}
        </div>
      )}

      <div className="feature-name" style={{ marginBottom: 6 }}>
        Логи
      </div>
      {logs ? (
        <pre
          style={{
            background: 'var(--surface-inset)',
            borderRadius: 12,
            padding: 12,
            fontSize: 11.5,
            color: 'var(--text-secondary)',
            maxHeight: 220,
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
          }}
        >
          {logs || 'Логов пока нет.'}
        </pre>
      ) : (
        <div className="feature-desc">Логов нет — бот не запускался в этой сессии.</div>
      )}
    </div>
  )
}

function FeaturesTab({
  botId,
  features,
  template,
  onChanged,
}: {
  botId: number
  features: FeatureStatusItem[]
  template: string | null
  onChanged: () => void
}) {
  const [openFeature, setOpenFeature] = useState<string | null>(null)
  const [dropdownOpen, setDropdownOpen] = useState(false)

  if (features.length === 0) {
    return <div className="state-message">Для этого бота пока нет доступных фич.</div>
  }

  const enabledCount = features.filter((f) => f.state === 'on').length

  const toggleFeature = (f: FeatureStatusItem) => {
    if (f.state === 'on') {
      disableFeature(botId, f.name).then(onChanged)
    } else if (f.name === 'office_events') {
      // office_events has its own bot-picker UI (the "Офисы" tab) — no
      // free-text configure step, so the checkbox here just points the
      // owner there instead of opening a chat the backend would reject
      // with 400 (see _NO_FREE_TEXT_FEATURES in factory_analytics_api.py).
      window.alert('Настраивается на вкладке «Офисы» — выбери, какой бот уведомлять.')
    } else {
      setOpenFeature(openFeature === f.name ? null : f.name)
    }
  }

  return (
    <div>
      <div className="feature-dropdown">
        <button className="feature-dropdown-trigger" onClick={() => setDropdownOpen(!dropdownOpen)}>
          <span>Функции ({enabledCount} из {features.length} включено)</span>
          <span>{dropdownOpen ? '▲' : '▼'}</span>
        </button>
        {dropdownOpen && (
          <div className="feature-dropdown-list">
            {features.map((f) => (
              <label key={f.name} className="feature-dropdown-item">
                <input
                  type="checkbox"
                  checked={f.state === 'on'}
                  ref={(el) => {
                    if (el) el.indeterminate = f.state === 'pending'
                  }}
                  onChange={() => toggleFeature(f)}
                />
                <span>{featureLabel(f.name)}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      {features.map((f) => (
        <div key={f.name}>
          {f.description && f.state === 'on' && (
            <div className="feature-desc" style={{ marginTop: 4 }}>
              {featureLabel(f.name)}: «{f.description}»
            </div>
          )}
          {openFeature === f.name && f.state !== 'on' && f.name !== 'office_events' && (
            <FeatureConfigureSubpanel
              botId={botId}
              feature={f}
              template={template}
              onDone={() => {
                setOpenFeature(null)
                onChanged()
              }}
              onCancel={() => {
                setOpenFeature(null)
                onChanged()
              }}
            />
          )}
          {f.state === 'on' &&
            f.name !== 'payments' &&
            f.name !== 'office_events' &&
            f.description && (
              <div className="feature-subpanel">
                <div className="row">
                  <span>Настроено</span>
                  <button className="mini-btn" onClick={() => setOpenFeature(f.name)}>
                    Изменить описание
                  </button>
                </div>
              </div>
            )}
        </div>
      ))}
    </div>
  )
}

function FeatureConfigureSubpanel({
  botId,
  feature,
  onDone,
  onCancel,
}: {
  botId: number
  feature: FeatureStatusItem
  template: string | null
  onDone: () => void
  onCancel: () => void
}) {
  const [step, setStep] = useState<'guide' | 'chat'>(feature.thread.length > 0 ? 'chat' : 'guide')
  const [message, setMessage] = useState('')
  const [thread, setThread] = useState(feature.thread)
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const guide = FEATURE_GUIDES[feature.name]

  if (feature.name === 'payments') {
    return (
      <div className="feature-subpanel">
        <div>💳 Оплата настраивается через ЮKassa-мастер в Telegram-боте — открой Creator и нажми «Как подключить оплату».</div>
        <div className="row">
          <button className="mini-btn danger" onClick={onCancel}>
            Закрыть
          </button>
        </div>
      </div>
    )
  }

  const send = async () => {
    if (!message.trim()) return
    setSending(true)
    setError(null)
    try {
      const result = await configureFeature(botId, feature.name, message.trim())
      if (result.status === 'enabled') {
        onDone()
        return
      }
      setThread(
        result.thread || [...thread, { role: 'owner', text: message.trim() }, { role: 'claude', text: result.reply }],
      )
      setMessage('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Не удалось отправить сообщение')
    } finally {
      setSending(false)
    }
  }

  const cancel = () => {
    cancelFeatureConfigure(botId, feature.name).finally(onCancel)
  }

  if (step === 'guide' && guide) {
    return (
      <div className="feature-subpanel">
        <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{featureLabel(feature.name)}</div>
        <div>
          <b>Что это даст:</b> {guide.what}
        </div>
        <div>
          <b>Что нужно будет сделать:</b>
          <ol style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {guide.steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </div>
        <div className="row">
          <button className="mini-btn" onClick={() => setStep('chat')}>
            Продолжить →
          </button>
          <button className="mini-btn danger" onClick={cancel}>
            Отмена
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="feature-subpanel">
      {thread.map((turn, i) => (
        <div key={i} className={`thread-turn ${turn.role}`}>
          {turn.text}
        </div>
      ))}
      {error && <div className="thread-turn claude">{error}</div>}
      <textarea
        className="feature-config-textarea"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder={FEATURE_QUESTION[feature.name] || 'Опиши, что нужно настроить'}
        rows={4}
        style={{ width: '100%' }}
      />
      <div className="row">
        <button className="mini-btn" disabled={sending || !message.trim()} onClick={send}>
          Отправить
        </button>
        <button className="mini-btn danger" onClick={cancel}>
          Отмена
        </button>
      </div>
    </div>
  )
}

const EVENT_TYPE_LABELS: Record<string, string> = {
  'order.created': 'Новый заказ',
  'task.assigned': 'Задача назначена',
}

function eventTypeLabel(eventType: string): string {
  return EVENT_TYPE_LABELS[eventType] || eventType
}

type WizardStep = 'source' | 'target' | 'event_type' | 'confirm' | 'success'

function OfficesTab({
  botId,
  offices,
  allBots,
  onChanged,
}: {
  botId: number
  offices: { source_bot_id: number; target_bot_id: number; event_type: string }[]
  allBots: FactoryBotItem[]
  onChanged: () => void
}) {
  const [wizardOpen, setWizardOpen] = useState(false)
  const nameById = (id: number) =>
    allBots.find((b) => b.id === id)?.display_name || allBots.find((b) => b.id === id)?.name || `#${id}`

  return (
    <div>
      {offices.length === 0 && (
        <div className="feature-desc" style={{ marginBottom: 8 }}>
          Пока нет связей с другими ботами.
        </div>
      )}
      {offices.map((link) => {
        const isSource = link.source_bot_id === botId
        const otherId = isSource ? link.target_bot_id : link.source_bot_id
        return (
          <div
            className="office-link-row"
            key={`${link.source_bot_id}-${link.target_bot_id}-${link.event_type}`}
          >
            <span>
              <span className="office-arrow">{isSource ? '📤' : '📥'}</span> {isSource ? '→' : '←'}{' '}
              {nameById(otherId)}
              <span className="feature-desc"> · {eventTypeLabel(link.event_type)}</span>
            </span>
            {isSource && (
              <button
                className="remove-x"
                onClick={() => removeOffice(botId, link.target_bot_id, link.event_type).then(onChanged)}
              >
                ✕
              </button>
            )}
          </div>
        )
      })}
      {wizardOpen ? (
        <OfficeLinkWizard
          defaultSourceId={botId}
          allBots={allBots}
          onDone={() => {
            setWizardOpen(false)
            onChanged()
          }}
          onCancel={() => setWizardOpen(false)}
        />
      ) : (
        <button className="add-fab" onClick={() => setWizardOpen(true)}>
          ➕ Связать ботов
        </button>
      )}
    </div>
  )
}

function OfficeLinkWizard({
  defaultSourceId,
  allBots,
  onDone,
  onCancel,
}: {
  defaultSourceId: number
  allBots: FactoryBotItem[]
  onDone: () => void
  onCancel: () => void
}) {
  const [step, setStep] = useState<WizardStep>('source')
  const [sourceId, setSourceId] = useState<number | null>(defaultSourceId)
  const [targetId, setTargetId] = useState<number | null>(null)
  const [eventTypes, setEventTypes] = useState<{ event_type: string; label: string }[] | null>(null)
  const [eventType, setEventType] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showcaseOffered, setShowcaseOffered] = useState<boolean | null>(null)

  const botName = (id: number | null) => {
    if (id == null) return ''
    const b = allBots.find((x) => x.id === id)
    return b ? b.display_name || b.name : `#${id}`
  }

  const loadEventTypes = (srcId: number) => {
    setEventTypes(null)
    setError(null)
    listOfficeEventTypes(srcId)
      .then((r) => setEventTypes(r.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить типы событий'))
  }

  const confirm = () => {
    if (sourceId == null || targetId == null || !eventType) return
    setSubmitting(true)
    setError(null)
    addOffice(sourceId, targetId, eventType)
      .then(() => {
        getShowcaseGroupStatus()
          .then((r) => setShowcaseOffered(!r.connected))
          .catch(() => setShowcaseOffered(true))
        setStep('success')
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось создать связь'))
      .finally(() => setSubmitting(false))
  }

  if (step === 'success') {
    return (
      <div className="feature-subpanel">
        <div>
          ✅ «{botName(sourceId)}» теперь автоматически уведомляет «{botName(targetId)}» о событии «
          {eventType ? eventTypeLabel(eventType) : ''}».
        </div>
        {showcaseOffered && <ShowcaseGroupGuide onDismiss={() => setShowcaseOffered(false)} />}
        <div className="row">
          <button className="mini-btn" onClick={onDone}>
            Готово
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="feature-subpanel">
      {error && <div>{error}</div>}

      {step === 'source' && (
        <>
          <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Шаг 1 из 3 — какой бот источник?</div>
          {allBots.map((b) => (
            <button
              key={b.id}
              className="mini-btn"
              style={{ marginBottom: 6, width: '100%' }}
              onClick={() => {
                setSourceId(b.id)
                setTargetId(null)
                setEventType(null)
                loadEventTypes(b.id)
                setStep('target')
              }}
            >
              {b.display_name || b.name}
            </button>
          ))}
          <div className="row">
            <button className="mini-btn danger" onClick={onCancel}>
              Отмена
            </button>
          </div>
        </>
      )}

      {step === 'target' && sourceId != null && (
        <>
          <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
            Шаг 2 из 3 — кого уведомлять от «{botName(sourceId)}»?
          </div>
          {allBots.filter((b) => b.id !== sourceId).length === 0 && <div>Других ботов пока нет.</div>}
          {allBots
            .filter((b) => b.id !== sourceId)
            .map((b) => (
              <button
                key={b.id}
                className="mini-btn"
                style={{ marginBottom: 6, width: '100%' }}
                onClick={() => {
                  setTargetId(b.id)
                  setStep('event_type')
                }}
              >
                {b.display_name || b.name}
              </button>
            ))}
          <div className="row">
            <button className="mini-btn danger" onClick={() => setStep('source')}>
              Назад
            </button>
          </div>
        </>
      )}

      {step === 'event_type' && (
        <>
          <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Шаг 3 из 3 — какое событие?</div>
          {eventTypes === null && <div>Загрузка…</div>}
          {eventTypes !== null && eventTypes.length === 0 && (
            <div>«{botName(sourceId)}» не поддерживает ни одного типа события для связи.</div>
          )}
          {eventTypes?.map((et) => (
            <button
              key={et.event_type}
              className="mini-btn"
              style={{ marginBottom: 6, width: '100%' }}
              onClick={() => {
                setEventType(et.event_type)
                setStep('confirm')
              }}
            >
              {et.label}
            </button>
          ))}
          <div className="row">
            <button className="mini-btn danger" onClick={() => setStep('target')}>
              Назад
            </button>
          </div>
        </>
      )}

      {step === 'confirm' && eventType && (
        <>
          <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>Что произойдёт</div>
          <div>
            Бот «{botName(sourceId)}» будет автоматически уведомлять бота «{botName(targetId)}» о событии «
            {eventTypeLabel(eventType)}». Это работает через сервер — боты не должны состоять в одной группе
            Telegram. Задержка — доли секунды.
          </div>
          <div className="row">
            <button className="mini-btn" disabled={submitting} onClick={confirm}>
              Подтвердить
            </button>
            <button className="mini-btn danger" onClick={() => setStep('event_type')}>
              Назад
            </button>
          </div>
        </>
      )}
    </div>
  )
}

function ShowcaseGroupGuide({ onDismiss }: { onDismiss: () => void }) {
  return (
    <div style={{ marginTop: 4, paddingTop: 8, borderTop: '1px solid var(--border-hair, #333)' }}>
      <div style={{ fontWeight: 600, color: 'var(--text-primary)', marginBottom: 4 }}>
        🔔 Показать в Telegram-группе
      </div>
      <div className="feature-desc">
        1) Создайте новую Telegram-группу
        <br />
        2) Добавьте туда Creator-бота
        <br />
        3) Готово — он будет присылать сюда сводку по связанным событиям
        <br />
        <br />
        Клиентские боты в группу добавлять не нужно — они не участвуют в доставке событий, только Creator
        показывает read-only сводку.
      </div>
      <div className="row">
        <button className="mini-btn danger" onClick={onDismiss}>
          Скрыть
        </button>
      </div>
    </div>
  )
}

function AdminsTab({ botId, admins, onChanged }: { botId: number; admins: string[]; onChanged: () => void }) {
  const [adding, setAdding] = useState(false)
  const [telegramId, setTelegramId] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = () => {
    if (!telegramId.trim().match(/^\d+$/)) {
      setError('Telegram ID — это число')
      return
    }
    addAdmin(botId, telegramId.trim())
      .then(() => {
        setAdding(false)
        setTelegramId('')
        setError(null)
        onChanged()
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось добавить'))
  }

  return (
    <div>
      {admins.length === 0 && (
        <div className="feature-desc" style={{ marginBottom: 8 }}>
          Дополнительных админов нет.
        </div>
      )}
      {admins.map((id) => (
        <div className="admin-row" key={id}>
          <div className="admin-avatar">👤</div>
          <div style={{ flex: 1 }}>
            <div className="admin-id">{id}</div>
          </div>
          <button className="remove-x" onClick={() => removeAdmin(botId, id).then(onChanged)}>
            ✕
          </button>
        </div>
      ))}
      {adding ? (
        <div className="feature-subpanel">
          <input
            value={telegramId}
            onChange={(e) => setTelegramId(e.target.value)}
            placeholder="Telegram ID"
            style={{ width: '100%' }}
          />
          {error && <div>{error}</div>}
          <div className="row">
            <button className="mini-btn" onClick={submit}>
              Добавить
            </button>
            <button className="mini-btn danger" onClick={() => setAdding(false)}>
              Отмена
            </button>
          </div>
        </div>
      ) : (
        <button className="add-fab" onClick={() => setAdding(true)}>
          + Добавить админа по Telegram ID
        </button>
      )}
    </div>
  )
}

// Owner support-session record editor — "Данные" tab. Scenario: a client
// wrote "не получается сделать X" and the owner needs to see and fix the
// actual stuck record (a booking, an order) without touching code. Reuses
// the resource/field shape runtime/factory_analytics_api.py's
// resource_schema_handler exposes (same miniapp_config authoring contract
// templates already declare for the customer-facing mini-app), but through
// the owner-only /api/factory/bots/{bot_id}/... path — deliberately UI-
// driven point edit/delete, not raw SQL access (see project memory: owner
// confirmed "нужно уметь много чего менять" but point edit through UI is
// safer than a SQL console).
function DataTab({ botId }: { botId: number }) {
  const [resources, setResources] = useState<FactorySchemaResource[] | null>(null)
  const [schemaError, setSchemaError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    setResources(null)
    setSchemaError(null)
    setSelected(null)
    getBotSchema(botId)
      .then((data) => {
        setResources(data.resources)
        setSelected(data.resources[0]?.name ?? null)
      })
      .catch((err) => setSchemaError(err instanceof ApiError ? err.message : 'Не удалось загрузить схему данных'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [botId])

  if (schemaError) return <div className="state-message">{schemaError}</div>
  if (resources === null) return <div className="state-message">Загрузка…</div>
  if (resources.length === 0) return <div className="state-message">У этого бота нет данных для редактирования.</div>

  const resource = resources.find((r) => r.name === selected) ?? resources[0]

  return (
    <div>
      {resources.length > 1 && (
        <div className="row" style={{ flexWrap: 'wrap', marginBottom: 8 }}>
          {resources.map((r) => (
            <button
              key={r.name}
              className={`mini-btn ${r.name === resource.name ? 'active' : ''}`}
              onClick={() => setSelected(r.name)}
            >
              {r.title || r.name}
            </button>
          ))}
        </div>
      )}
      <ResourceRecordsList key={resource.name} botId={botId} resource={resource} />
    </div>
  )
}

function ResourceRecordsList({ botId, resource }: { botId: number; resource: FactorySchemaResource }) {
  const [items, setItems] = useState<FactoryResourceItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<number | null>(null)

  const load = () => {
    setItems(null)
    setError(null)
    listBotResource(botId, resource.name)
      .then((data) => setItems(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось загрузить записи'))
  }

  useEffect(load, [botId, resource.name])

  const handleDelete = (item: FactoryResourceItem) => {
    const label = String(item[resource.titleField || 'id'] ?? `#${item.id}`)
    if (!window.confirm(`Удалить запись «${label}»? Это необратимо.`)) return
    deleteBotResource(botId, resource.name, item.id)
      .then(load)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось удалить запись'))
  }

  if (error) return <div className="state-message">{error}</div>
  if (items === null) return <div className="state-message">Загрузка…</div>
  if (items.length === 0) return <div className="state-message">Пока пусто.</div>

  const listFields = resource.fields.filter((f) => (f.list ?? false) === true)

  return (
    <div>
      {items.map((item) => (
        <div className="admin-row" key={item.id} style={{ alignItems: 'flex-start', flexDirection: 'column', gap: 6 }}>
          {editingId === item.id ? (
            <ResourceEditForm
              botId={botId}
              resource={resource}
              item={item}
              onSaved={() => {
                setEditingId(null)
                load()
              }}
              onCancel={() => setEditingId(null)}
            />
          ) : (
            <>
              <div style={{ width: '100%' }}>
                <div className="admin-id">
                  {String(item[resource.titleField || 'id'] ?? `#${item.id}`)}
                </div>
                {listFields.length > 0 && (
                  <div className="feature-desc">
                    {listFields
                      .filter((f) => item[f.name] != null && item[f.name] !== '')
                      .map((f) => `${f.label || f.name}: ${String(item[f.name])}`)
                      .join(' · ')}
                  </div>
                )}
              </div>
              <div className="row">
                <button className="mini-btn" onClick={() => setEditingId(item.id)}>
                  Изменить
                </button>
                <button className="mini-btn danger" onClick={() => handleDelete(item)}>
                  Удалить
                </button>
              </div>
            </>
          )}
        </div>
      ))}
    </div>
  )
}

function ResourceEditForm({
  botId,
  resource,
  item,
  onSaved,
  onCancel,
}: {
  botId: number
  resource: FactorySchemaResource
  item: FactoryResourceItem
  onSaved: () => void
  onCancel: () => void
}) {
  const editableFields = resource.fields.filter((f) => f.name !== 'id')
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {}
    for (const f of editableFields) {
      const raw = item[f.name]
      initial[f.name] = raw == null ? '' : String(raw)
    }
    return initial
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = () => {
    setSaving(true)
    setError(null)
    const payload: Record<string, unknown> = {}
    for (const f of editableFields) {
      const raw = values[f.name]
      payload[f.name] = f.kind === 'number' ? Number(raw) : raw
    }
    updateBotResource(botId, resource.name, item.id, payload)
      .then(onSaved)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Не удалось сохранить'))
      .finally(() => setSaving(false))
  }

  return (
    <div className="feature-subpanel" style={{ width: '100%' }}>
      {error && <div>{error}</div>}
      {editableFields.map((f) => (
        <div className="field" key={f.name}>
          <label htmlFor={`edit-${f.name}`}>{f.label || f.name}</label>
          <input
            id={`edit-${f.name}`}
            type={f.kind === 'number' ? 'number' : f.kind === 'date' ? 'date' : 'text'}
            value={values[f.name] ?? ''}
            onChange={(e) => setValues((prev) => ({ ...prev, [f.name]: e.target.value }))}
            style={{ width: '100%' }}
          />
        </div>
      ))}
      <div className="row">
        <button className="mini-btn" disabled={saving} onClick={submit}>
          {saving ? 'Сохранение…' : 'Сохранить'}
        </button>
        <button className="mini-btn danger" disabled={saving} onClick={onCancel}>
          Отмена
        </button>
      </div>
    </div>
  )
}

function MaintenanceTab({
  botId,
  bot,
  busy,
  setBusy,
  setActionError,
  onChanged,
}: {
  botId: number
  bot: FactoryBotItem | null
  busy: boolean
  setBusy: (b: boolean) => void
  setActionError: (e: string | null) => void
  onChanged: () => void
}) {
  const [fixDescribing, setFixDescribing] = useState(false)
  const [bugText, setBugText] = useState('')
  const [fixPreview, setFixPreview] = useState<FixBugPreview | null>(null)
  const [rating, setRating] = useState(false)

  const run = async (fn: () => Promise<{ ok: boolean; error: string | null }>) => {
    setBusy(true)
    setActionError(null)
    try {
      const result = await fn()
      if (!result.ok) {
        setActionError(result.error || 'Не удалось выполнить')
      }
      onChanged()
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Не удалось выполнить')
    } finally {
      setBusy(false)
    }
  }

  const generateFix = async () => {
    setBusy(true)
    setActionError(null)
    try {
      const preview = await previewFixBug(botId, bugText.trim())
      if (!preview.ok || !preview.fixed_code) {
        setActionError(preview.error || 'Не удалось сгенерировать исправление')
        return
      }
      setFixPreview(preview)
      setFixDescribing(false)
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Не удалось сгенерировать исправление')
    } finally {
      setBusy(false)
    }
  }

  const confirmFix = async () => {
    if (!fixPreview?.fixed_code) return
    await run(() => applyFixBug(botId, fixPreview.fixed_code as string, fixPreview.main_code_hash))
    setFixPreview(null)
    setBugText('')
  }

  const cancelFix = () => {
    setFixPreview(null)
    setBugText('')
  }

  return (
    <div>
      <button className="maint-btn" disabled={busy} onClick={() => run(() => autofixBot(botId))}>
        <span className="ic">🔍</span>
        <div>
          <div>Авто-диагностика</div>
          <div className="maint-sub">найти и объяснить ошибки</div>
        </div>
      </button>
      {fixPreview ? (
        <div className="feature-subpanel">
          <div>{fixPreview.explanation}</div>
          <div className="maint-sub">Проверь и подтверди применение.</div>
          <div className="row">
            <button className="mini-btn" disabled={busy} onClick={() => confirmFix()}>
              ✅ Применить
            </button>
            <button className="mini-btn danger" disabled={busy} onClick={cancelFix}>
              ❌ Отмена
            </button>
          </div>
        </div>
      ) : fixDescribing ? (
        <div className="feature-subpanel">
          <textarea
            value={bugText}
            onChange={(e) => setBugText(e.target.value)}
            placeholder="Опиши баг или что нужно улучшить"
            rows={2}
            style={{ width: '100%' }}
          />
          <div className="row">
            <button className="mini-btn" disabled={!bugText.trim() || busy} onClick={() => generateFix()}>
              Отправить
            </button>
            <button className="mini-btn danger" onClick={() => setFixDescribing(false)}>
              Отмена
            </button>
          </div>
        </div>
      ) : (
        <button className="maint-btn" disabled={busy} onClick={() => setFixDescribing(true)}>
          <span className="ic">🐛</span>
          <div>
            <div>Исправить баг</div>
            <div className="maint-sub">описать проблему словами</div>
          </div>
        </button>
      )}
      <button className="maint-btn" disabled={busy} onClick={() => run(() => recreateBot(botId))}>
        <span className="ic">🔄</span>
        <div>
          <div>Перегенерировать</div>
          <div className="maint-sub">немного улучшим код</div>
        </div>
      </button>
      {bot &&
        (rating ? (
          <FeedbackForm bot={bot} onDone={() => { setRating(false); onChanged() }} />
        ) : (
          <button className="maint-btn" disabled={busy} onClick={() => setRating(true)}>
            <span className="ic">⭐</span>
            <div>
              <div>Оценить</div>
              <div className="maint-sub">оставить оценку и комментарий</div>
            </div>
          </button>
        ))}
    </div>
  )
}
