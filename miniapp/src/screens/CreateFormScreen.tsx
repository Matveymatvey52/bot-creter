import { useCallback, useEffect, useState } from 'react'
import { createResource, listResource, ApiError, type ResourceItem } from '../lib/api'
import type { FieldDisplay, ResourceDisplay } from '../lib/displaySchema'
import { useTelegramMainButton } from '../lib/useMainButton'
import { isInTelegram } from '../lib/telegram'
import { CTAButton } from '../components/CTAButton'

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
  // Options for foreign-key fields, keyed by referenced resource name. A
  // field like `tour_id` must offer "Сочи, ноябрь", never ask for the number 7.
  const [refOptions, setRefOptions] = useState<Record<string, ResourceItem[]>>({})

  useEffect(() => {
    let cancelled = false
    const targets = new Set(resource.createFields.filter((f) => f.ref).map((f) => f.ref!.resource))
    if (targets.size === 0) return

    Promise.all(
      [...targets].map((refResource) =>
        listResource(refResource)
          .then((data) => [refResource, data.items] as const)
          // An unreadable referenced list leaves the picker empty rather than
          // silently degrading back to a raw id input.
          .catch(() => [refResource, [] as ResourceItem[]] as const),
      ),
    ).then((results) => {
      if (cancelled) return
      setRefOptions(Object.fromEntries(results))
    })
    return () => {
      cancelled = true
    }
  }, [resource])

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
        payload[f.name] = f.kind === 'number' || f.ref ? Number(raw) : raw
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

  const setValue = (name: string, value: string) => setValues((prev) => ({ ...prev, [name]: value }))

  return (
    <div className="screen">
      <div className="screen-header-v2">
        <div className="screen-header-v2-titles">
          <span className="label-caps">{resource.title}</span>
          <h1 className="display-lg">Новая запись</h1>
        </div>
        <CTAButton variant="secondary" onClick={onCancel}>
          ✕
        </CTAButton>
      </div>

      {error && <div className="state-message">{error}</div>}

      {resource.createFields.map((f) => (
        <div className="field" key={f.name}>
          <label htmlFor={f.name}>{f.label}</label>
          <FormInput
            field={f}
            value={values[f.name] ?? ''}
            options={f.ref ? (refOptions[f.ref.resource] ?? []) : []}
            onChange={(value) => setValue(f.name, value)}
          />
        </div>
      ))}

      {!isInTelegram() && (
        <div className="bottom-bar">
          <CTAButton variant="primary" onClick={handleSubmit} disabled={!requiredFilled || submitting}>
            {submitting ? 'Создание…' : 'Создать'}
          </CTAButton>
        </div>
      )}
    </div>
  )
}

function FormInput({
  field,
  value,
  options,
  onChange,
}: {
  field: FieldDisplay
  value: string
  options: ResourceItem[]
  onChange: (value: string) => void
}) {
  if (field.ref) {
    const labelField = field.ref.labelField
    return (
      <select id={field.name} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— выберите —</option>
        {options.map((option) => (
          <option key={option.id} value={String(option.id)}>
            {String(option[labelField] ?? `#${option.id}`)}
          </option>
        ))}
      </select>
    )
  }

  if (field.kind === 'username') {
    // The "@" is decoration, not part of the value — the backend strips and
    // lowercases whatever arrives (services/client_link.py), so typing it or
    // not both work; showing it just makes the expected format obvious.
    return (
      <div className="input-prefixed">
        <span className="input-prefix" aria-hidden="true">
          @
        </span>
        <input
          id={field.name}
          type="text"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          placeholder="username"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>
    )
  }

  return (
    <input
      id={field.name}
      type={field.kind === 'number' ? 'number' : field.kind === 'date' ? 'date' : 'text'}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}
