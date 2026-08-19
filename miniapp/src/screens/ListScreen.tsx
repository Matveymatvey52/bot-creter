import { useEffect, useState } from 'react'
import { listResource, ApiError, type ResourceItem } from '../lib/api'
import { statusTone, type ResourceDisplay } from '../lib/displaySchema'
import { Card, CardHeader, CardTitle, ChipRow, Chip, Badge } from '../components/Card'
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

  return (
    <div className="screen">
      <ScreenHeader eyebrow={resource.title} title={resource.title} avatarUrl={getTelegramUserPhotoUrl()} />

      {error && <div className="state-message">{error}</div>}
      {!error && items === null && <div className="state-message">Загрузка…</div>}
      {!error && items !== null && items.length === 0 && (
        <div className="state-message">Пока пусто</div>
      )}

      {items?.map((item) => (
        <Card key={item.id} onClick={() => onOpenItem(item.id)}>
          <CardHeader>
            <CardTitle>{String(item[resource.titleField] ?? `#${item.id}`)}</CardTitle>
            {'status' in item && <Badge tone={statusTone(item.status)}>{String(item.status ?? '—')}</Badge>}
          </CardHeader>
          <ChipRow>
            {resource.listFields
              .filter((f) => f.name !== 'status' && item[f.name] != null && item[f.name] !== '')
              .map((f) => (
                <Chip key={f.name}>{String(item[f.name])}</Chip>
              ))}
          </ChipRow>
        </Card>
      ))}

      <div className="cta-row">
        <CTAButton icon="➕" variant="primary" onClick={onCreateNew}>
          Добавить
        </CTAButton>
      </div>
    </div>
  )
}
