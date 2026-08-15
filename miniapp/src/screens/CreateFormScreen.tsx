import { useCallback, useState } from 'react'
import { createResource, ApiError } from '../lib/api'
import type { ResourceDisplay } from '../lib/displaySchema'
import { useTelegramMainButton } from '../lib/useMainButton'
import { isInTelegram } from '../lib/telegram'

export function CreateFormScreen({
  resource,
  onCreated,
  onCancel,
}: {
  resource: ResourceDisplay
  onCreated: (id: number) => void
  onCancel: () => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const requiredFilled = resource.createFields.every((f) => f.name !== 'name' || values.name?.trim())

  const handleSubmit = useCallback(async () => {
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {}
      for (const f of resource.createFields) {
        const raw = values[f.name]
        if (raw === undefined || raw === '') continue
        payload[f.name] = f.kind === 'number' ? Number(raw) : raw
      }
      const result = await createResource(resource.name, payload)
      onCreated(result.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Не удалось создать запись')
    } finally {
      setSubmitting(false)
    }
  }, [resource, submitting, values, onCreated])

  useTelegramMainButton('Создать', handleSubmit, requiredFilled && !submitting)

  return (
    <div className="screen">
      <div className="screen-header">
        <button className="btn-secondary" onClick={onCancel}>
          ✕
        </button>
        <h2>{resource.title}: новая запись</h2>
      </div>

      {error && <div className="state-message">{error}</div>}

      {resource.createFields.map((f) => (
        <div className="field" key={f.name}>
          <label htmlFor={f.name}>{f.label}</label>
          <input
            id={f.name}
            type={f.kind === 'number' ? 'number' : f.kind === 'date' ? 'date' : 'text'}
            value={values[f.name] ?? ''}
            onChange={(e) => setValues((prev) => ({ ...prev, [f.name]: e.target.value }))}
          />
        </div>
      ))}

      {!isInTelegram() && (
        <div className="bottom-bar">
          <button className="btn-primary" onClick={handleSubmit} disabled={!requiredFilled || submitting}>
            {submitting ? 'Создание…' : 'Создать'}
          </button>
        </div>
      )}
    </div>
  )
}
