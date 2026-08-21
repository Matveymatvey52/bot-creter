/* One record, everything about it, on one screen.

   The previous version showed a title, a status badge and a handful of
   `meta-row` strings — for a tour that meant the program, hotels, guests and
   cashflow all lived in sibling tabs the user had to open separately and
   cross-reference by id. Now the backend joins those sub-records onto the
   detail response (miniapp_api.py's `related`) and they render inline here,
   as real tables; foreign keys resolve to names, and URLs are tappable. */

import { useCallback, useEffect, useState } from 'react'
import { getResource, listResource, ApiError, type RelatedSection, type ResourceItem } from '../lib/api'
import { statusTone, type ResourceDisplay } from '../lib/displaySchema'
import { Badge } from '../components/Card'
import { CTAButton } from '../components/CTAButton'
import { DataTable, type TableColumn } from '../components/DataTable'
import { FieldValue, refLabelKey, type RefLabels } from '../components/FieldValue'
import { SectionLabel } from '../components/SectionLabel'
import { useTelegramMainButton } from '../lib/useMainButton'
import { isInTelegram } from '../lib/telegram'

export function DetailScreen({
  resource,
  resources,
  itemId,
  onBack,
}: {
  resource: ResourceDisplay
  resources: Record<string, ResourceDisplay>
  itemId: number
  onBack: () => void
}) {
  const [item, setItem] = useState<ResourceItem | null>(null)
  const [related, setRelated] = useState<RelatedSection[]>([])
  const [refLabels, setRefLabels] = useState<RefLabels>({})
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setItem(null)
    setRelated([])
    setError(null)
    getResource(resource.name, itemId)
      .then((data) => {
        if (cancelled) return
        setItem(data.item)
        setRelated(data.related ?? [])
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : 'Не удалось загрузить запись')
      })
    return () => {
      cancelled = true
    }
  }, [resource.name, itemId])

  // Foreign keys are stored as ids but must never be SHOWN as ids. Each
  // referenced resource is fetched once and turned into an id→title map; a
  // referenced list this viewer can't read simply leaves the label unresolved
  // rather than failing the screen.
  useEffect(() => {
    let cancelled = false
    const refs = resource.detailFields.filter((f) => f.ref)
    if (refs.length === 0) return

    const targets = new Map(refs.map((f) => [f.ref!.resource, f.ref!.labelField]))
    Promise.all(
      [...targets].map(([refResource, labelField]) =>
        listResource(refResource)
          .then((data) =>
            data.items.map(
              (row) => [refLabelKey(refResource, row.id), String(row[labelField] ?? row.id)] as const,
            ),
          )
          .catch(() => []),
      ),
    ).then((results) => {
      if (cancelled) return
      setRefLabels(Object.fromEntries(results.flat()))
    })
    return () => {
      cancelled = true
    }
  }, [resource])

  const handlePrimaryAction = useCallback(() => onBack(), [onBack])
  useTelegramMainButton('Назад к списку', handlePrimaryAction, Boolean(item))

  return (
    <div className="screen">
      <div className="cta-row">
        <CTAButton icon="←" variant="secondary" onClick={onBack}>
          Назад
        </CTAButton>
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
            .filter((f) => f.name !== 'status' && f.name !== resource.titleField)
            .map((f) => (
              <div className="meta-row" key={f.name}>
                <span>{f.label}</span>
                <FieldValue field={f} value={item[f.name]} refLabels={refLabels} />
              </div>
            ))}
        </div>
      )}

      {item &&
        related.map((section) => (
          <RelatedBlock key={section.resource} section={section} resources={resources} />
        ))}

      {item && !isInTelegram() && (
        <div className="bottom-bar">
          <CTAButton variant="primary" onClick={onBack}>
            Назад к списку
          </CTAButton>
        </div>
      )}
    </div>
  )
}

/* One `children` section of the parent record. Columns come from the child
   resource's own list metadata when the schema exposes it (so a template
   controls what a guest row shows the same way it controls the guests tab),
   and fall back to whatever columns the rows actually carry for configs that
   declare children without display metadata. */
function RelatedBlock({
  section,
  resources,
}: {
  section: RelatedSection
  resources: Record<string, ResourceDisplay>
}) {
  const childResource = resources[section.resource]
  const columns = relatedColumns(section, childResource)

  if (section.as === 'list' && childResource) {
    return (
      <div className="screen-section">
        <SectionLabel>{section.title}</SectionLabel>
        <div className="related-list">
          {section.items.length === 0 && <div className="state-message">Пока пусто</div>}
          {section.items.map((row) => (
            <div className="related-list-row" key={row.id}>
              <div className="related-list-title">
                {String(row[childResource.titleField] ?? `#${row.id}`)}
              </div>
              <div className="related-list-meta">
                {columns
                  .filter(
                    (c) => c.name !== childResource.titleField && row[c.name] != null && row[c.name] !== '',
                  )
                  .map((c) => (
                    <span key={c.name}>
                      {c.label}: {String(row[c.name])}
                    </span>
                  ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="screen-section">
      <SectionLabel>{section.title}</SectionLabel>
      <DataTable columns={columns} rows={section.items} />
    </div>
  )
}

function relatedColumns(
  section: RelatedSection,
  childResource: ResourceDisplay | undefined,
): TableColumn[] {
  if (childResource) {
    const fields =
      childResource.listFields.length > 0 ? childResource.listFields : childResource.detailFields
    // A foreign key here is the join back to the parent whose card we're
    // already on — showing it in every row is noise, and it would be an id.
    const shown = fields.filter((f) => !f.ref && f.name !== childResource.titleField)
    if (shown.length > 0 || childResource.titleField) {
      return [
        { name: childResource.titleField, label: childResource.title },
        ...shown.map((f) => ({ name: f.name, label: f.label })),
      ]
    }
  }
  const sample = section.items[0]
  if (!sample) return []
  return Object.keys(sample)
    .filter((k) => k !== 'id')
    .map((k) => ({ name: k, label: k }))
}
