/* The engine's one table primitive: a real <table> with a column header and
   rows, wrapped in its own horizontally-scrolling container so a wide table
   never forces the page itself to scroll sideways.

   Used for any tabular data the config declares — a resource marked
   `tableView`, and every `children` section on a detail card (a tour's
   cashflow, a task's attachments). Cells render values verbatim; formatting
   decisions belong to whoever authored the column, not here. */

import type { ReactNode } from 'react'
import type { ResourceItem } from '../lib/api'

export interface TableColumn {
  name: string
  label: string
  // Класс выравнивания колонки: числа вправо, даты приглушённо. Ставится
  // тем, кто собирает колонки, — он знает kind поля, а таблица нет.
  className?: string
  render?: (value: unknown, row: ResourceItem) => ReactNode
}

export function DataTable({
  columns,
  rows,
  onRowClick,
}: {
  columns: TableColumn[]
  rows: ResourceItem[]
  onRowClick?: (row: ResourceItem) => void
}) {
  if (rows.length === 0) {
    return <div className="state-message">Пока пусто</div>
  }

  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col.name} className={col.className}>
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={onRowClick ? 'data-table-row-clickable' : undefined}
            >
              {columns.map((col) => {
                const value = row[col.name]
                return (
                  <td key={col.name} className={col.className}>
                    {col.render
                      ? col.render(value, row)
                      : value != null && value !== ''
                        ? String(value)
                        : '—'}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
