import { useEffect, useState } from 'react'
import {
  listResource,
  ApiError,
  PARENT_NONE,
  type ListScope,
  type ResourceItem,
} from '../lib/api'
import { statusToneRich, type FieldDisplay, type ResourceDisplay } from '../lib/displaySchema'
import { DataTable, type TableColumn } from '../components/DataTable'
import { refLabelKey, type RefLabels } from '../components/FieldValue'
import { TotalsStrip } from '../components/TotalsStrip'
import { Icon } from '../components/Icon'
import { ParentPickerScreen } from './ParentPickerScreen'

/* Раскладка карточки — вариант I (утверждён владельцем 2026-08-21,
   эталон design/mockups/miniapp_mockup_I.html): заголовок и подпись слева,
   числовое значение и статус-точка справа, снизу полоса фактов.

   Набор полей у каждого бота свой, поэтому слоты заполняются по `kind` из
   miniapp_config, а не по именам полей:
     date   → подпись под заголовком
     number → значение справа
     прочие → до трёх ячеек в полосе фактов

   Поля-ссылки (`ref`) сюда не попадают вообще: они хранят чужой id, и в
   карточке это выглядело бы как голое число. Человекочитаемое название
   такой связи показывает детальный экран, который умеет его разрешить. */
const MONTHS = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]

// Даты бот хранит как умеет; человеку «2026-08-22» читать неудобно, а в
// эталоне стоит «12 – 19 сентября · 8 ночей». Разбираем терпимо: что датой не
// оказалось — показываем как есть, а не прячем.
function parseDay(value: unknown): Date | null {
  if (typeof value !== 'string') return null
  const m = value.match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!m) return null
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  return Number.isNaN(d.getTime()) ? null : d
}

function formatPeriod(startRaw: unknown, endRaw: unknown): string {
  const start = parseDay(startRaw)
  const end = parseDay(endRaw)
  if (!start) return startRaw == null ? '' : String(startRaw)
  const startText = `${start.getDate()} ${MONTHS[start.getMonth()]}`
  if (!end || end.getTime() <= start.getTime()) return startText
  const sameMonth = start.getMonth() === end.getMonth() && start.getFullYear() === end.getFullYear()
  const range = sameMonth
    ? `${start.getDate()} – ${end.getDate()} ${MONTHS[end.getMonth()]}`
    : `${startText} – ${end.getDate()} ${MONTHS[end.getMonth()]}`
  const nights = Math.round((end.getTime() - start.getTime()) / 86_400_000)
  const word = nights % 10 === 1 && nights % 100 !== 11 ? 'ночь'
    : [2, 3, 4].includes(nights % 10) && ![12, 13, 14].includes(nights % 100) ? 'ночи'
    : 'ночей'
  return `${range} · ${nights} ${word}`
}

// Справа в карточке стоит ДЕНЕЖНАЯ величина — так в эталоне. Просто «первое
// числовое поле» туда ставить нельзя: у тура первым числом идёт количество
// гостей, и карточка показывала «8» на месте бюджета. Опознаём деньги по
// названию поля и его подписи; всё остальное числовое уходит в факты, где ему
// и место.
const MONEY_RE = /cost|price|amount|sum|total|budget|payment|revenue|стоим|сумм|цен|бюджет|оплат|выручк|доход|расход/i

function isMoney(field: FieldDisplay): boolean {
  return field.kind === 'number' && (MONEY_RE.test(field.name) || MONEY_RE.test(field.label))
}

/* Раскладка карточки — вариант I (утверждён владельцем 2026-08-21,
   эталон design/mockups/miniapp_mockup_I.html): заголовок и подпись слева,
   денежная величина и статус-точка справа, снизу полоса фактов.

   Набор полей у каждого бота свой, поэтому слоты заполняются по `kind` из
   miniapp_config, а не по именам полей:
     date   → подпись под заголовком (две даты складываются в период)
     деньги → значение справа
     прочие → до трёх ячеек в полосе фактов

   Поля-ссылки (`ref`) сюда не попадают вообще: они хранят чужой id, и в
   карточке это выглядело бы как голое число. Человекочитаемое название
   такой связи показывает детальный экран, который умеет его разрешить. */
function splitFields(fields: FieldDisplay[], item: ResourceItem) {
  const filled = fields.filter(
    (f) => f.name !== 'status' && !f.ref && item[f.name] != null && item[f.name] !== '',
  )
  const dates = filled.filter((f) => f.kind === 'date')
  const amount = filled.find(isMoney)
  const subText = dates.length > 0 ? formatPeriod(item[dates[0].name], item[dates[1]?.name]) : ''
  const facts = filled.filter((f) => !dates.includes(f) && f !== amount).slice(0, 3)
  return { subText, amount, facts }
}

/* Что читается в панели контекста. Имя родителя берём из scope.options, а не
   из строк: тогда пустой отфильтрованный список всё равно назван — а пустой
   раздел это ровно тот случай, когда вопрос «чьё это?» звучит громче всего. */
function contextLabel(scope: ListScope, parent: string | null): string {
  if (parent === null) return `Все · сводно`
  if (parent === PARENT_NONE) return 'Не привязано'
  const option = scope.options.find((o) => o.id === parent)
  return option ? (option.label ?? `#${option.id}`) : '—'
}

export function ListScreen({
  resource,
  onOpenItem,
  onCreateNew,
}: {
  resource: ResourceDisplay
  onOpenItem: (id: number) => void
  onCreateNew: () => void
}) {
  const [items, setItems] = useState<ResourceItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Названия связанных записей для ref-колонок таблицы: «Тур» обязан показывать
  // «Сочи», а не id 12. Раздел, недоступный этому зрителю, просто остаётся без
  // подписи — экран из-за этого не ломается.
  const [refLabels, setRefLabels] = useState<RefLabels>({})
  /* Чем ограничен этот список — как это сообщил бэкенд. У глобального ресурса
     остаётся null: блок scope не приходит вовсе, поэтому здесь нет состояния,
     из-за которого справочник мог бы нарисовать себе родителя
     (docs/SCOPE_AUDIT_STAGE_A.md). */
  const [scope, setScope] = useState<ListScope | null>(null)
  const [parent, setParent] = useState<string | null>(null)
  const [picking, setPicking] = useState(false)

  /* Смена раздела не тащит за собой прежнего родителя: id тура ничего не
     значит для списка сотрудников. */
  useEffect(() => {
    setParent(null)
    setPicking(false)
  }, [resource.name])

  useEffect(() => {
    let cancelled = false
    setItems(null)
    setError(null)
    listResource(resource.name, parent)
      .then((data) => {
        if (cancelled) return
        setItems(data.items)
        setScope(data.scope ?? null)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить данные')
      })
    return () => {
      cancelled = true
    }
  }, [resource.name, parent])

  useEffect(() => {
    let cancelled = false
    const refs = resource.listFields.filter((f) => f.ref)
    if (refs.length === 0) return
    const targets = new Map(refs.map((f) => [f.ref!.resource, f.ref!.labelField]))
    Promise.all(
      [...targets].map(([refResource, labelField]) =>
        listResource(refResource)
          .then((data) =>
            data.items.map(
              (row) => [refLabelKey(refResource, row.id), String(row[labelField] ?? row.id)] as const,
            ),
          )
          .catch(() => []),
      ),
    ).then((results) => {
      if (!cancelled) setRefLabels(Object.fromEntries(results.flat()))
    })
    return () => {
      cancelled = true
    }
  }, [resource])

  // Колонка суммы, которую шаблон объявил знаковой: рисуем со знаком и
  // цветом — приход зелёным, расход красным, как в эталоне.
  const signedTotal = resource.totals.find((t) => t.signBy)

  const tableColumns: TableColumn[] = [
    { name: resource.titleField, label: resource.title },
    ...resource.listFields
      .filter((f) => f.name !== resource.titleField)
      .map((f): TableColumn => {
        if (f.ref) {
          const ref = f.ref
          return {
            name: f.name,
            label: f.label,
            render: (value) =>
              value == null || value === ''
                ? '—'
                : (refLabels[refLabelKey(ref.resource, value)] ?? String(value)),
          }
        }
        if (signedTotal && f.name === signedTotal.field) {
          const { field: signField, positive } = signedTotal.signBy!
          return {
            name: f.name,
            label: f.label,
            className: 'col-number',
            render: (value, row) => {
              const n = Number(value)
              if (!Number.isFinite(n)) return '—'
              const income = String(row[signField]) === positive
              const text = new Intl.NumberFormat('ru-RU').format(Math.abs(n))
              return (
                <span className={income ? 'num pos' : 'num neg'}>
                  {income ? '+' : '−'}
                  {text}
                </span>
              )
            },
          }
        }
        if (f.kind === 'status') {
          // Цвет нужен и в таблице: статус — это состояние записи, а не
          // просто слово. Та же точка и тот же тон, что на карточке.
          return {
            name: f.name,
            label: f.label,
            render: (value) =>
              value == null || value === '' ? (
                '—'
              ) : (
                <span className={`sol-st tone-${statusToneRich(value)}`}>
                  <span className="sol-dot" />
                  {String(value)}
                </span>
              ),
          }
        }
        return {
          name: f.name,
          label: f.label,
          className: f.kind === 'number' ? 'col-number' : f.kind === 'date' ? 'col-date' : undefined,
        }
      }),
  ]

  if (picking && scope !== null) {
    return (
      <ParentPickerScreen
        scope={scope}
        sectionTitle={resource.title}
        selected={parent}
        onSelect={(next) => {
          setParent(next)
          setPicking(false)
        }}
        onCancel={() => setPicking(false)}
      />
    )
  }

  return (
    <div className="screen">
      {/* Панель контекста из эталона (design/mockups/miniapp_mockup_I.html,
          .ctxbar): раздел всегда говорит, чьи записи показывает, и здесь же
          меняется. Справочник на том же месте говорит, что родителя у него
          нет, — молчание выглядело бы как непомеченный scoped-раздел. */}
      {scope !== null && (
        <div className="ctxbar" onClick={() => setPicking(true)} role="button" tabIndex={0}>
          <div className="ctx-x">
            <div className="ctx-k">{scope.sectionLabel}</div>
            <div className="ctx-v">
              <span className="ctx-dot" />
              {contextLabel(scope, parent)}
            </div>
          </div>
          <span className="ctx-sw">
            Сменить
            <Icon name="chevron" />
          </span>
        </div>
      )}
      {scope === null && (
        <div className="ctx-glob">Общий справочник — не зависит от родителя</div>
      )}

      <div className="sol-head">
        <div>
          <h1>{resource.title}</h1>
          <div className="sol-head-count">
            {items === null ? 'загрузка…' : `записей: ${items.length}`}
          </div>
        </div>
        {/* Кнопка создания появляется, только если бэкенд подтвердил, что POST
            этого зрителя действительно пройдёт (canCreate). Показывать форму,
            которая ответит 403 «resource is read-only», — ровно тот баг, ради
            которого эта проверка и заведена. */}
        {resource.canCreate && (
          <button className="sol-add" onClick={onCreateNew} aria-label="Добавить">
            <Icon name="plus" />
          </button>
        )}
      </div>

      {error && <div className="state-message">{error}</div>}
      {!error && items === null && <div className="state-message">Загрузка…</div>}
      {!error && items !== null && items.length === 0 && (
        <div className="state-message">Пока пусто</div>
      )}

      {items !== null && items.length > 0 && resource.tableView && (
        <>
          <DataTable columns={tableColumns} rows={items} onRowClick={(row) => onOpenItem(row.id)} />
          <TotalsStrip totals={resource.totals} rows={items} />
        </>
      )}

      {items !== null && items.length > 0 && !resource.tableView && (
        <div className="sol-list">
          {items.map((item) => {
            const { subText, amount, facts } = splitFields(resource.listFields, item)
            const tone = statusToneRich(item.status)
            return (
              <button key={item.id} className="sol-item" onClick={() => onOpenItem(item.id)}>
                <div className="sol-item-top">
                  <div>
                    <div className="sol-item-name">
                      {String(item[resource.titleField] ?? `#${item.id}`)}
                    </div>
                    {subText && <div className="sol-item-sub">{subText}</div>}
                  </div>
                  <div className="sol-item-right">
                    {amount && <div className="sol-item-amt">{String(item[amount.name])}</div>}
                    {'status' in item && (
                      <div style={{ marginTop: amount ? 6 : 0 }}>
                        <span className={`sol-st tone-${tone}`}>
                          <span className="sol-dot" />
                          {String(item.status ?? '—')}
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                {facts.length > 0 && (
                  <div className="sol-facts">
                    {facts.map((f) => (
                      <div className="sol-fact" key={f.name}>
                        <div className="sol-fact-k">{f.label}</div>
                        <div className="sol-fact-v">{String(item[f.name])}</div>
                      </div>
                    ))}
                  </div>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
