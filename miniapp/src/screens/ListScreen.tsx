import { useEffect, useState } from 'react'
import { listResource, ApiError, type ResourceItem } from '../lib/api'
import { statusToneRich, type FieldDisplay, type ResourceDisplay } from '../lib/displaySchema'
import { Icon } from '../components/Icon'

/* Раскладка карточки — вариант I (утверждён владельцем 2026-08-21,
   эталон design/mockups/miniapp_mockup_I.html): заголовок и подпись слева,
   числовое значение и статус-точка справа, снизу полоса фактов.

   Набор полей у каждого бота свой, поэтому слоты заполняются по `kind` из
   miniapp_config, а не по именам полей:
     date   → подпись под заголовком
     number → значение справа
     прочие → до трёх ячеек в полосе фактов */
function splitFields(fields: FieldDisplay[], item: ResourceItem) {
  const filled = fields.filter(
    (f) => f.name !== 'status' && item[f.name] != null && item[f.name] !== '',
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

  return (
    <div className="screen">
      <div className="sol-head">
        <div>
          <h1>{resource.title}</h1>
          <div className="sol-head-count">
            {items === null ? 'загрузка…' : `записей: ${items.length}`}
          </div>
        </div>
        <button className="sol-add" onClick={onCreateNew} aria-label="Добавить">
          <Icon name="plus" />
        </button>
      </div>

      {error && <div className="state-message">{error}</div>}
      {!error && items !== null && items.length === 0 && (
        <div className="state-message">Пока пусто</div>
      )}

      {items !== null && items.length > 0 && (
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
