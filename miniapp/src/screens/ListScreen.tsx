import { useEffect, useState } from 'react'
import { listResource, ApiError, type ResourceItem } from '../lib/api'
import { statusTone, type ResourceDisplay } from '../lib/displaySchema'
import { Card, CardHeader, CardTitle, ChipRow, Chip, Badge } from '../components/Card'
import { DataTable, type TableColumn } from '../components/DataTable'
import { ScreenHeader } from '../components/ScreenHeader'
import { CTAButton } from '../components/CTAButton'
import { getTelegramUserPhotoUrl } from '../lib/telegram'

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

  const tableColumns: TableColumn[] = [
    { name: resource.titleField, label: resource.title },
    ...resource.listFields
      .filter((f) => f.name !== resource.titleField && !f.ref)
      .map((f) => ({ name: f.name, label: f.label })),
  ]

  return (
    <div className="screen">
      <ScreenHeader eyebrow={resource.title} title={resource.title} avatarUrl={getTelegramUserPhotoUrl()} />

      {error && <div className="state-message">{error}</div>}
      {!error && items === null && <div className="state-message">Загрузка…</div>}
      {!error && items !== null && items.length === 0 && <div className="state-message">Пока пусто</div>}

      {items !== null && items.length > 0 && resource.tableView && (
        <DataTable columns={tableColumns} rows={items} onRowClick={(row) => onOpenItem(row.id)} />
      )}

      {items !== null &&
        !resource.tableView &&
        items.map((item) => (
          <Card key={item.id} onClick={() => onOpenItem(item.id)}>
            <CardHeader>
              <CardTitle>{String(item[resource.titleField] ?? `#${item.id}`)}</CardTitle>
              {'status' in item && <Badge tone={statusTone(item.status)}>{String(item.status ?? '—')}</Badge>}
            </CardHeader>
            <ChipRow>
              {resource.listFields
                .filter((f) => f.name !== 'status' && !f.ref && item[f.name] != null && item[f.name] !== '')
                .map((f) => (
                  <Chip key={f.name}>{String(item[f.name])}</Chip>
                ))}
            </ChipRow>
          </Card>
        ))}

      {/* No create affordance unless the backend confirmed this viewer's POST
          would actually succeed — offering a form that answers 403 "resource
          is read-only" is the exact failure this gate exists to prevent. */}
      {resource.canCreate && (
        <div className="cta-row">
          <CTAButton icon="➕" variant="primary" onClick={onCreateNew}>
            Добавить
          </CTAButton>
        </div>
      )}
    </div>
  )
}
