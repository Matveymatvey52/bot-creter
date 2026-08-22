import { useEffect, useState } from 'react'
import { listResource, ApiError, type ResourceItem } from '../lib/api'
import { statusToneRich, type FieldDisplay, type ResourceDisplay } from '../lib/displaySchema'
import { DataTable, type TableColumn } from '../components/DataTable'
import { refLabelKey, type RefLabels } from '../components/FieldValue'
import { TotalsStrip } from '../components/TotalsStrip'
import { Icon } from '../components/Icon'

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
function splitFields(fields: FieldDisplay[], item: ResourceItem) {
  const filled = fields.filter(
    (f) => f.name !== 'status' && !f.ref && item[f.name] != null && item[f.name] !== '',
  )
  const sub = filled.find((f) => f.kind === 'date')
  const amount = filled.find((f) => f.kind === 'number')
  const facts = filled.filter((f) => f !== sub && f !== amount).slice(0, 3)
  return { sub, amount, facts }
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

  useEffect(() => {
    let cancelled = false
    setItems(null)
    setError(null)
    listResource(resource.name)
      .then((data) => {
        if (!cancelled) setItems(data.items)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить данные')
      })
    return () => {
      cancelled = true
    }
  }, [resource.name])

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
        return { name: f.name, label: f.label }
      }),
  ]

  return (
    <div className="screen">
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
            const { sub, amount, facts } = splitFields(resource.listFields, item)
            const tone = statusToneRich(item.status)
            return (
              <button key={item.id} className="sol-item" onClick={() => onOpenItem(item.id)}>
                <div className="sol-item-top">
                  <div>
                    <div className="sol-item-name">
                      {String(item[resource.titleField] ?? `#${item.id}`)}
                    </div>
                    {sub && <div className="sol-item-sub">{String(item[sub.name])}</div>}
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
