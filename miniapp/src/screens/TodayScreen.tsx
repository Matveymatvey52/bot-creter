import type { ResourceItem } from '../lib/api'
import { statusToneRich, type ResourceDisplay } from '../lib/displaySchema'
import { Icon, iconForResource } from '../components/Icon'
import { TotalsStrip } from '../components/TotalsStrip'

/* «Сегодня» — единственный экран, отвечающий на вопрос «что у меня сейчас».
   Лента сверху водит между разделами, но нигде не видно ближайших дат и общих
   сумм сразу — этот экран закрывает именно эту дыру, а не повторяет ленту.

   Работает у ЛЮБОГО бота без правки его конфига: даты находит по kind:'date',
   деньги — по колонкам, объявленным в totals. Чего в схеме нет, того на экране
   нет: пустых обещаний не рисуем. */

// Даты приходят строками в том виде, в каком их хранит бот: ISO, «12.09.2026»,
// иногда вовсе не дата. Разбираем терпимо и молча пропускаем то, что датой не
// является — сортировать по мусору хуже, чем не показать его вовсе.
function parseDate(value: unknown): number | null {
  if (typeof value !== 'string' || value.trim() === '') return null
  const iso = Date.parse(value)
  if (!Number.isNaN(iso)) return iso
  const ru = value.match(/^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})$/)
  if (ru) {
    const year = ru[3].length === 2 ? 2000 + Number(ru[3]) : Number(ru[3])
    const t = Date.UTC(year, Number(ru[2]) - 1, Number(ru[1]))
    return Number.isNaN(t) ? null : t
  }
  return null
}

interface Upcoming {
  resource: string
  title: string
  item: ResourceItem
  when: number
  whenText: string
}

const DAY = 86_400_000

export function TodayScreen({
  resources,
  datasets,
  onOpenItem,
}: {
  resources: Record<string, ResourceDisplay>
  datasets: Record<string, ResourceItem[]>
  onOpenItem: (resource: string, itemId: number) => void
}) {
  // Вчерашнее ещё показываем: сегодняшняя работа обычно про него же.
  const since = Date.now() - DAY

  const upcoming: Upcoming[] = []
  for (const [name, rows] of Object.entries(datasets)) {
    const resource = resources[name]
    if (!resource) continue
    const dateFields = resource.listFields
      .concat(resource.detailFields)
      .filter((f) => f.kind === 'date')
    if (dateFields.length === 0) continue
    for (const item of rows) {
      // У записи может быть несколько дат (начало и конец) — берём ближайшую
      // из ещё не прошедших, иначе запись всплывала бы по давно прошедшей.
      let best: { when: number; text: string } | null = null
      for (const f of dateFields) {
        const when = parseDate(item[f.name])
        if (when === null || when < since) continue
        if (!best || when < best.when) best = { when, text: String(item[f.name]) }
      }
      if (best) {
        upcoming.push({
          resource: name,
          title: String(item[resource.titleField] ?? `#${item.id}`),
          item,
          when: best.when,
          whenText: best.text,
        })
      }
    }
  }
  upcoming.sort((a, b) => a.when - b.when)

  const withTotals = Object.entries(datasets).filter(
    ([name, rows]) => resources[name]?.totals.length && rows.length > 0,
  )

  return (
    <div className="screen">
      <div className="sol-head">
        <div>
          <h1>Сегодня</h1>
          <div className="sol-head-count">
            {upcoming.length > 0 ? `ближайших записей: ${upcoming.length}` : 'ближайших записей нет'}
          </div>
        </div>
      </div>

      {withTotals.map(([name, rows]) => (
        <div className="screen-section" key={name}>
          <div className="sol-block-h">{resources[name].title}</div>
          <TotalsStrip totals={resources[name].totals} rows={rows} />
        </div>
      ))}

      {upcoming.length > 0 && (
        <div className="screen-section">
          <div className="sol-block-h">Ближайшее</div>
          <div className="sol-sheet">
            {upcoming.slice(0, 12).map((u) => (
              <button
                className="sol-mrow"
                key={`${u.resource}:${u.item.id}`}
                onClick={() => onOpenItem(u.resource, Number(u.item.id))}
              >
                <Icon name={iconForResource(u.resource, resources[u.resource].title)} size={15} />
                <span className="sol-mrow-n">{u.title}</span>
                {u.item.status != null && u.item.status !== '' && (
                  <span className={`sol-st tone-${statusToneRich(u.item.status)}`}>
                    <span className="sol-dot" />
                    {String(u.item.status)}
                  </span>
                )}
                <span className="sol-mrow-c">{u.whenText}</span>
                <Icon name="chevron" size={14} />
              </button>
            ))}
          </div>
        </div>
      )}

      {upcoming.length === 0 && withTotals.length === 0 && (
        <div className="state-message">
          Здесь появятся ближайшие даты и итоги, как только в разделах будут записи.
        </div>
      )}
    </div>
  )
}
