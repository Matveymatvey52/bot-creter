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
  fixBug,
  disableFeature,
  configureFeature,
  cancelFeatureConfigure,
  addOffice,
  removeOffice,
  listOfficeEventTypes,
  getShowcaseGroupStatus,
  addAdmin,
  removeAdmin,
  ApiError,
  type BotDetail,
  type FeatureStatusItem,
  type FactoryBotItem,
} from '../lib/factoryApi'
import { iconForTemplate } from '../lib/botIcons'

type Tab = 'overview' | 'features' | 'offices' | 'admins' | 'maintenance'

const TABS: { key: Tab; label: string }[] = [
  { key: 'overview', label: 'Обзор' },
  { key: 'features', label: 'Фичи' },
  { key: 'offices', label: 'Офисы' },
  { key: 'admins', label: 'Админы' },
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
              {detail.template || 'from-scratch'}
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
        {tab === 'maintenance' && (
          <MaintenanceTab botId={botId} busy={busy} setBusy={setBusy} setActionError={setActionError} onChanged={load} />
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

function OverviewTab({ botId, detail }: { botId: number; detail: BotDetail }) {
  const [logs, setLogs] = useState<string | null>(null)
  const enabledCount = detail.features.filter((f) => f.state === 'on').length

  useEffect(() => {
    getBotLogs(botId)
      .then((r) => setLogs(r.logs))
      .catch(() => setLogs(null))
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

  if (features.length === 0) {
    return <div className="state-message">Для этого бота пока нет доступных фич.</div>
  }

  return (
    <div>
      {features.map((f) => (
        <div key={f.name}>
          <div className="feature-row">
            <div>
              <div className="feature-name">{featureLabel(f.name)}</div>
              {f.description && f.state === 'on' && <div className="feature-desc">«{f.description}»</div>}
            </div>
            <button
              className={`switch ${f.state === 'on' ? 'on' : f.state === 'pending' ? 'pending' : ''}`}
              onClick={() => {
                if (f.state === 'on') {
                  disableFeature(botId, f.name).then(onChanged)
                } else if (f.name === 'office_events') {
                  // office_events has its own bot-picker UI (the "Офисы" tab)
                  // — no free-text configure step, so the tumbler here just
                  // points the owner there instead of opening a chat that
                  // the backend would reject with 400 (see
                  // _NO_FREE_TEXT_FEATURES in factory_analytics_api.py).
                  window.alert('Настраивается на вкладке «Офисы» — выбери, какой бот уведомлять.')
                } else {
                  setOpenFeature(openFeature === f.name ? null : f.name)
                }
              }}
            />
          </div>
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
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder={FEATURE_QUESTION[feature.name] || 'Опиши, что нужно настроить'}
        rows={2}
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

function MaintenanceTab({
  botId,
  busy,
  setBusy,
  setActionError,
  onChanged,
}: {
  botId: number
  busy: boolean
  setBusy: (b: boolean) => void
  setActionError: (e: string | null) => void
  onChanged: () => void
}) {
  const [fixDescribing, setFixDescribing] = useState(false)
  const [bugText, setBugText] = useState('')

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

  return (
    <div>
      <button className="maint-btn" disabled={busy} onClick={() => run(() => autofixBot(botId))}>
        <span className="ic">🔍</span>
        <div>
          <div>Авто-диагностика</div>
          <div className="maint-sub">найти и объяснить ошибки</div>
        </div>
      </button>
      {fixDescribing ? (
        <div className="feature-subpanel">
          <textarea
            value={bugText}
            onChange={(e) => setBugText(e.target.value)}
            placeholder="Опиши баг или что нужно улучшить"
            rows={2}
            style={{ width: '100%' }}
          />
          <div className="row">
            <button
              className="mini-btn"
              disabled={!bugText.trim() || busy}
              onClick={() => {
                run(() => fixBug(botId, bugText.trim()))
                setFixDescribing(false)
                setBugText('')
              }}
            >
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
    </div>
  )
}
