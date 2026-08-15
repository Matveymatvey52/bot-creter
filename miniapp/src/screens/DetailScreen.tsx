import { useCallback, useEffect, useState } from 'react'
import { getResource, ApiError, type ResourceItem } from '../lib/api'
import { statusTone, type ResourceDisplay } from '../lib/displaySchema'
import { Badge } from '../components/Card'
import { useTelegramMainButton } from '../lib/useMainButton'
import { isInTelegram } from '../lib/telegram'

export function DetailScreen({
  resource,
  itemId,
  onBack,
}: {
  resource: ResourceDisplay
  itemId: number
  onBack: () => void
}) {
  const [item, setItem] = useState<ResourceItem | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setItem(null)
    setError(null)
    getResource(resource.name, itemId)
      .then((data) => {
        if (!cancelled) setItem(data.item)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить запись')
      })
    return () => {
      cancelled = true
    }
  }, [resource.name, itemId])

  // Pilot's one primary action is "back to list" — a real per-template
  // action (e.g. "отметить оплаченным") is out of scope until a second
  // pilot resource needs one; see docs/MINIAPP_DESIGN.md §3 screen types.
  const handlePrimaryAction = useCallback(() => onBack(), [onBack])
  useTelegramMainButton('Назад к списку', handlePrimaryAction, Boolean(item))

  return (
    <div className="screen">
      <div className="screen-header">
        <button className="btn-secondary" onClick={onBack}>
          ← Назад
        </button>
      </div>

      {error && <div className="state-message">{error}</div>}
      {!error && !item && <div className="state-message">Загрузка…</div>}

      {item && (
        <div className="card">
          <div className="card-header">
            <div className="card-title">{String(item[resource.titleField] ?? `#${item.id}`)}</div>
            {'status' in item && <Badge tone={statusTone(item.status)}>{String(item.status ?? '—')}</Badge>}
          </div>
          <hr className="divider" />
          {resource.detailFields
            .filter((f) => f.name !== 'status')
            .map((f) => (
              <div className="meta-row" key={f.name}>
                <span>{f.label}</span>
                <span>{item[f.name] != null && item[f.name] !== '' ? String(item[f.name]) : '—'}</span>
              </div>
            ))}
        </div>
      )}

      {item && !isInTelegram() && (
        <div className="bottom-bar">
          <button className="btn-primary" onClick={onBack}>
            Назад к списку
          </button>
        </div>
      )}
    </div>
  )
}
