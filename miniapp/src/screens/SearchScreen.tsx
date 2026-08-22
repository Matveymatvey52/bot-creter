import { useState } from 'react'
import type { ResourceItem } from '../lib/api'
import { statusToneRich, type ResourceDisplay } from '../lib/displaySchema'
import { Icon, iconForResource } from '../components/Icon'

/* Поиск по всем разделам сразу. До него найти запись можно было только глазами
   и только в том разделе, где она лежит, — а человек обычно помнит имя, но не
   раздел.

   Ищем по уже загруженным строкам: за ними всё равно ходили запросы для
   счётчиков разделов, так что поиск не добавляет ни одного обращения к бэкенду
   и отвечает мгновенно. Обратная сторона честная: он видит ровно то, что видит
   приложение — если раздел не отдался, его записей в выдаче не будет. */

interface Hit {
  resource: string
  item: ResourceItem
  title: string
  hint: string
}

export function SearchScreen({
  resources,
  datasets,
  onOpenItem,
}: {
  resources: Record<string, ResourceDisplay>
  datasets: Record<string, ResourceItem[]>
  onOpenItem: (resource: string, itemId: number) => void
}) {
  const [query, setQuery] = useState('')
  const needle = query.trim().toLowerCase()

  const hits: Hit[] = []
  if (needle !== '') {
    for (const [name, rows] of Object.entries(datasets)) {
      const resource = resources[name]
      if (!resource) continue
      // Ищем по названию записи и по полям, которые бот сам счёл достойными
      // списка: искать по всем колонкам подряд — значит находить по служебным
      // отметкам времени и чужим id.
      const searchable = [resource.titleField, ...resource.listFields.map((f) => f.name)]
      for (const item of rows) {
        const hay = searchable
          .map((n) => (item[n] == null ? '' : String(item[n])))
          .join(' ')
          .toLowerCase()
        if (!hay.includes(needle)) continue
        const hint = resource.listFields
          .filter(
            (f) => f.name !== resource.titleField && item[f.name] != null && item[f.name] !== '',
          )
          .slice(0, 2)
          .map((f) => `${f.label}: ${String(item[f.name])}`)
          .join(' · ')
        hits.push({
          resource: name,
          item,
          title: String(item[resource.titleField] ?? `#${item.id}`),
          hint,
        })
      }
    }
  }

  return (
    <div className="screen">
      <div className="sol-head">
        <div>
          <h1>Поиск</h1>
          <div className="sol-head-count">
            {needle === '' ? 'по всем разделам' : `найдено: ${hits.length}`}
          </div>
        </div>
      </div>

      <div className="sol-sheet">
        <div className="sol-spec">
          <label className="sol-spec-k" htmlFor="sol-search">
            Запрос
          </label>
          <span className="sol-spec-rt">
            <input
              className="sol-spec-in"
              id="sol-search"
              type="search"
              placeholder="имя, сумма, статус…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </span>
        </div>
      </div>

      {needle !== '' && hits.length === 0 && <div className="state-message">Ничего не найдено</div>}

      {hits.length > 0 && (
        <div className="sol-sheet">
          {hits.slice(0, 50).map((hit) => (
            <button
              className="sol-mrow"
              key={`${hit.resource}:${hit.item.id}`}
              onClick={() => onOpenItem(hit.resource, Number(hit.item.id))}
            >
              <Icon name={iconForResource(hit.resource, resources[hit.resource].title)} size={15} />
              <span className="sol-mrow-n">
                {hit.title}
                {hit.hint && <span className="sol-mrow-hint">{hit.hint}</span>}
              </span>
              {hit.item.status != null && hit.item.status !== '' && (
                <span className={`sol-st tone-${statusToneRich(hit.item.status)}`}>
                  <span className="sol-dot" />
                  {String(hit.item.status)}
                </span>
              )}
              <span className="sol-mrow-c">{resources[hit.resource].title}</span>
              <Icon name="chevron" size={14} />
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
