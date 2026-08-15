/* Client-side display schema, one entry per bot resource, keyed by resource
   name — see templates/tour_operator.py's miniapp_config for the
   server-side counterpart this mirrors. The backend's miniapp_config
   controls what columns EXIST and are writable; this controls how they're
   LABELED and formatted for a human, which the generic REST layer has no
   opinion on (see docs/MINIAPP_DESIGN.md §2.3: the backend stays
   template-agnostic, but a human-readable label is inherently per-domain).

   This is the only file that needs to change to point the SPA at a new
   bot's resources — see miniapp/README.md. Auto-generating this from
   miniapp_config server-side is tracked as Phase 2 (design doc §2.4) and
   hasn't landed yet, so it's maintained by hand today. */

export interface FieldDisplay {
  name: string
  label: string
  kind: 'text' | 'number' | 'date' | 'status'
}

export interface ResourceDisplay {
  name: string
  title: string
  titleField: string
  listFields: FieldDisplay[]
  detailFields: FieldDisplay[]
  createFields: FieldDisplay[]
}

export const RESOURCES: Record<string, ResourceDisplay> = {
  tours: {
    name: 'tours',
    title: 'Туры',
    titleField: 'name',
    listFields: [
      { name: 'destination', label: 'Направление', kind: 'text' },
      { name: 'status', label: 'Статус', kind: 'status' },
    ],
    detailFields: [
      { name: 'destination', label: 'Направление', kind: 'text' },
      { name: 'date_start', label: 'Начало', kind: 'date' },
      { name: 'date_end', label: 'Окончание', kind: 'date' },
      { name: 'guests_count', label: 'Гостей', kind: 'number' },
      { name: 'status', label: 'Статус', kind: 'status' },
    ],
    createFields: [
      { name: 'name', label: 'Название', kind: 'text' },
      { name: 'destination', label: 'Направление', kind: 'text' },
      { name: 'date_start', label: 'Дата начала', kind: 'date' },
      { name: 'date_end', label: 'Дата окончания', kind: 'date' },
      { name: 'guests_count', label: 'Кол-во гостей', kind: 'number' },
    ],
  },
  guests: {
    name: 'guests',
    title: 'Гости',
    titleField: 'name',
    listFields: [
      { name: 'status', label: 'Статус', kind: 'status' },
      { name: 'total_cost', label: 'Стоимость', kind: 'number' },
    ],
    detailFields: [
      { name: 'total_cost', label: 'Стоимость', kind: 'number' },
      { name: 'prepaid', label: 'Предоплата', kind: 'number' },
      { name: 'our_price', label: 'Наша цена', kind: 'number' },
      { name: 'status', label: 'Статус', kind: 'status' },
      { name: 'notes', label: 'Заметки', kind: 'text' },
    ],
    createFields: [
      { name: 'name', label: 'Имя гостя', kind: 'text' },
      { name: 'tour_id', label: 'ID тура', kind: 'number' },
      { name: 'total_cost', label: 'Стоимость', kind: 'number' },
      { name: 'prepaid', label: 'Предоплата', kind: 'number' },
    ],
  },
}

const PAID_STATUSES = new Set(['paid', 'active', 'confirmed'])

export function statusTone(status: unknown): 'success' | 'neutral' {
  return typeof status === 'string' && PAID_STATUSES.has(status) ? 'success' : 'neutral'
}
