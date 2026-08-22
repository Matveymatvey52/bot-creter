import type { ResourceItem, SchemaTotal } from '../lib/api'

/* Итоги под таблицей раздела — «Приход / Расход / Остаток» из эталона
   (design/mockups/miniapp_mockup_I.html).

   Считаются ТОЛЬКО по колонкам, которые шаблон объявил в "totals": сумма по
   всем числовым полям подряд сложила бы рубли с долларами и с id родителя.

   Если объявлен signBy — строки делятся на приход (значение поля равно
   positive) и расход, и показываются три плитки. Без signBy знака у чисел нет
   и делить нечего — тогда одна плитка «Итого». */

function fmt(n: number): string {
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(n)
}

function sum(rows: ResourceItem[], field: string): number {
  return rows.reduce((acc, row) => {
    const n = Number(row[field])
    return Number.isFinite(n) ? acc + n : acc
  }, 0)
}

export function TotalsStrip({ totals, rows }: { totals: SchemaTotal[]; rows: ResourceItem[] }) {
  if (totals.length === 0 || rows.length === 0) return null

  return (
    <>
      {totals.map((total) => {
        const suffix = total.label ? ` ${total.label}` : ''
        if (!total.signBy) {
          return (
            <div className="sol-totals" key={total.field}>
              <div className="sol-tot">
                <div className="sol-tot-k">Итого{suffix}</div>
                <div className="sol-tot-v">{fmt(sum(rows, total.field))}</div>
              </div>
            </div>
          )
        }
        const { field: signField, positive } = total.signBy
        const inSum = sum(
          rows.filter((r) => String(r[signField]) === positive),
          total.field,
        )
        const outSum = sum(
          rows.filter((r) => String(r[signField]) !== positive),
          total.field,
        )
        return (
          <div className="sol-totals" key={total.field}>
            <div className="sol-tot">
              <div className="sol-tot-k">Приход{suffix}</div>
              <div className="sol-tot-v pos">+{fmt(inSum)}</div>
            </div>
            <div className="sol-tot">
              <div className="sol-tot-k">Расход{suffix}</div>
              <div className="sol-tot-v neg">−{fmt(outSum)}</div>
            </div>
            <div className="sol-tot">
              <div className="sol-tot-k">Остаток{suffix}</div>
              <div className="sol-tot-v">{fmt(inSum - outSum)}</div>
            </div>
          </div>
        )
      })}
    </>
  )
}
