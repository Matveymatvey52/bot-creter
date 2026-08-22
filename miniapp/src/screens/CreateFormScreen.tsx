import { useCallback, useEffect, useState } from 'react'
import { createResource, listResource, ApiError, type ResourceItem } from '../lib/api'
import type { FieldDisplay, ResourceDisplay } from '../lib/displaySchema'
import { useTelegramMainButton } from '../lib/useMainButton'
import { Icon } from '../components/Icon'

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

  // Gates on the schema's own `required` flags — this used to hardcode a
  // single "name" field, so a resource whose required field was called
  // anything else let the user submit an empty form and get a 400 back.
  const requiredFilled = resource.createFields.every(
    (f) => !f.required || (values[f.name] ?? '').trim() !== '',
  )

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
      {/* Форма — тот же спец-лист, что и карточка записи (вариант I):
          подпись слева, поле ввода справа. Отдельного «бланка» с крупными
          полями в утверждённой раскладке нет. */}
      <div className="sol-sheet">
        <div className="sol-sheet-h">
          <button className="sol-crumb" onClick={onCancel}>
            <Icon name="back" size={13} />
            Отмена
          </button>
          <h1>Новая запись</h1>
          <div className="sol-sheet-sub">Раздел «{resource.title}»</div>
        </div>

        {resource.createFields.map((f) => (
          <div className="sol-spec" key={f.name}>
            <label className="sol-spec-k" htmlFor={f.name}>
              {f.label}
              {f.required && ' *'}
            </label>
            <span className="sol-spec-rt">
              <FormInput
                field={f}
                value={values[f.name] ?? ''}
                options={f.ref ? (refOptions[f.ref.resource] ?? []) : []}
                onChange={(value) => setValue(f.name, value)}
              />
              {f.ref && <Icon name="chevron" size={14} />}
            </span>
          </div>
        ))}
      </div>

      {error && <div className="state-message">{error}</div>}

      {/* Кнопка видна всегда, а не только вне Telegram: главная кнопка
          Telegram живёт за пределами страницы, и без этой строки внутри
          мессенджера форма выглядит как документ без действия. */}
      <div className="sol-acts">
        <button
          className="sol-btn pri"
          onClick={handleSubmit}
          disabled={!requiredFilled || submitting}
        >
          {submitting ? 'Создание…' : 'Создать'}
        </button>
      </div>
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
      <select className="sol-spec-in" id={field.name} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— выберите —</option>
        {options.map((option) => (
          <option key={option.id} value={String(option.id)}>
            {String(option[labelField] ?? `#${option.id}`)}
          </option>
        ))}
      </select>
    )
  }

  if (field.kind === 'contact') {
    // Free text on purpose: whatever the admin actually knows about this
    // person. Only an "@…" entry is held to Telegram's username rules by the
    // backend — a name or a phone is a perfectly good contact.
    return (
      <input
        className="sol-spec-in"
        id={field.name}
        type="text"
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        placeholder="@username или телефон"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }

  return (
    <input
      className="sol-spec-in"
      id={field.name}
      type={field.kind === 'number' ? 'number' : field.kind === 'date' ? 'date' : 'text'}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}
