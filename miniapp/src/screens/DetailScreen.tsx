/* Карточка одной записи — спец-лист варианта I (эталон
   design/mockups/miniapp_mockup_I.html).

   Все элементы эталона собраны из того, что движок реально знает о записи,
   а не зашиты под тур-оператора:
     kind:'status' → цветные теги под заголовком (в мокапе «planning» и
                     «оплачено 40%» — это ровно два status-поля);
     ref           → строка с шевроном, ведёт в карточку связанной записи;
     kind:'file'   → блок «Файлы и вложения» со своим экраном у каждого файла;
     children      → таблицы/списки связанных записей (в мокапе «Программа
                     тура») плюс кнопки-переходы в их разделы.
   Чего у записи нет — того на экране нет; пустых блоков-обещаний не рисуем. */

import { useCallback, useEffect, useState } from 'react'
import { getResource, listResource, ApiError, type RelatedSection, type ResourceItem } from '../lib/api'
import { statusToneRich, type FieldDisplay, type ResourceDisplay } from '../lib/displaySchema'
import { DataTable, type TableColumn } from '../components/DataTable'
import { FieldValue, refLabelKey, type RefLabels } from '../components/FieldValue'
import { Icon, iconForResource } from '../components/Icon'
import { useTelegramMainButton } from '../lib/useMainButton'

/* Иконка вложения по расширению — в эталоне у договора, ваучера и схемы
   рассадки разные глифы. Расширение берём из значения поля; чего не узнали,
   то рисуем нейтральным листом. */
function fileIcon(value: unknown): 'image' | 'receipt' | 'file' {
  const ext = String(value ?? '').toLowerCase().split('?')[0].split('.').pop() ?? ''
  if (['jpg', 'jpeg', 'png', 'webp', 'gif', 'heic'].includes(ext)) return 'image'
  if (['pdf'].includes(ext)) return 'file'
  if (['doc', 'docx', 'xls', 'xlsx', 'csv'].includes(ext)) return 'receipt'
  return 'file'
}

export function DetailScreen({
  resource,
  resources,
  itemId,
  onBack,
  onOpenRef,
  onOpenChild,
  onOpenFile,
}: {
  resource: ResourceDisplay
  resources: Record<string, ResourceDisplay>
  itemId: number
  onBack: () => void
  onOpenRef: (resource: string, itemId: number) => void
  onOpenChild: (resource: string) => void
  onOpenFile: (field: FieldDisplay, value: unknown) => void
}) {
  const [item, setItem] = useState<ResourceItem | null>(null)
  const [related, setRelated] = useState<RelatedSection[]>([])
  const [refLabels, setRefLabels] = useState<RefLabels>({})
  const [error, setError] = useState<string | null>(null)
  // Короткое подтверждение действия внизу экрана — вместо него нельзя молча
  // ничего не делать: строка со шевроном обязана отвечать на нажатие.
  const [note, setNote] = useState<string | null>(null)

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

  const copyValue = useCallback(async (label: string, value: unknown) => {
    const text = String(value ?? '')
    if (text === '') return
    try {
      await navigator.clipboard.writeText(text)
      setNote(`${label} скопировано`)
    } catch {
      setNote('Не удалось скопировать')
    }
    setTimeout(() => setNote(null), 1800)
  }, [])

  const filled = (f: FieldDisplay) => item != null && item[f.name] != null && item[f.name] !== ''

  // Подпись под заголовком — первая дата записи, как на карточке в списке.
  const subField = resource.detailFields.find((f) => f.kind === 'date')
  // Теги — все status-поля: у тура это и стадия, и состояние оплаты.
  const statusFields = resource.detailFields.filter((f) => f.kind === 'status')
  const fileFields = resource.detailFields.filter((f) => f.kind === 'file')
  const specFields = resource.detailFields.filter(
    (f) =>
      f !== subField &&
      f.name !== resource.titleField &&
      f.kind !== 'status' &&
      f.kind !== 'file',
  )

  return (
    <div className="screen">
      {error && <div className="state-message">{error}</div>}
      {!error && !item && <div className="state-message">Загрузка…</div>}

      {item && (
        <>
          <div className="sol-sheet">
            <div className="sol-sheet-h">
              <button className="sol-crumb" onClick={onBack}>
                <Icon name="back" size={13} />
                Все {resource.title.toLowerCase()}
              </button>
              <h1>{String(item[resource.titleField] ?? `#${item.id}`)}</h1>
              {subField && filled(subField) && (
                <div className="sol-sheet-sub">{String(item[subField.name])}</div>
              )}
              {statusFields.some(filled) && (
                <div className="sol-sheet-tags">
                  {statusFields.filter(filled).map((f) => (
                    <span className={`sol-st tone-${statusToneRich(item[f.name])}`} key={f.name}>
                      <span className="sol-dot" />
                      {String(item[f.name])}
                    </span>
                  ))}
                </div>
              )}
            </div>

            {specFields.map((f) => {
              const value = item[f.name]
              // В эталоне кликается КАЖДАЯ строка, и у каждой шеврон. Здесь то
              // же самое, но с настоящим действием: поле-ссылка ведёт в
              // карточку связанной записи, остальные копируют своё значение —
              // «нажал и ничего» было бы обманом шеврона.
              const target =
                f.ref && value != null && value !== '' && resources[f.ref.resource]
                  ? { resource: f.ref.resource, id: Number(value) }
                  : null
              return (
                <button
                  className="sol-spec"
                  key={f.name}
                  onClick={() =>
                    target ? onOpenRef(target.resource, target.id) : copyValue(f.label, value)
                  }
                >
                  <span className="sol-spec-k">{f.label}</span>
                  <span className="sol-spec-rt">
                    <span className="sol-spec-v">
                      <FieldValue field={f} value={value} refLabels={refLabels} />
                    </span>
                    <Icon name="chevron" size={14} />
                  </span>
                </button>
              )
            })}
          </div>

          {fileFields.some(filled) && (
            <div className="screen-section">
              <div className="sol-block-h">Файлы и вложения</div>
              <div className="sol-sheet">
                {fileFields.filter(filled).map((f) => (
                  <button
                    className="sol-frow"
                    key={f.name}
                    onClick={() => onOpenFile(f, item[f.name])}
                  >
                    <span className="sol-frow-ic">
                      <Icon name={fileIcon(item[f.name])} size={15} />
                    </span>
                    <span className="sol-frow-n">{String(item[f.name])}</span>
                    <Icon name="chevron" size={14} />
                  </button>
                ))}
              </div>
            </div>
          )}

          {related.map((section) => (
            <RelatedBlock key={section.resource} section={section} resources={resources} />
          ))}

          {/* Кнопки-переходы в разделы связанных записей — «Гости · 14» в
              эталоне. Считаем по фактически приехавшим строкам, а не по
              обещанию схемы. */}
          {note && <div className="state-message">{note}</div>}

          <div className="sol-acts">
            {related.length > 0 && (
              <div className="sol-acts-row">
                {related.map((section) => (
                  <button
                    className="sol-btn"
                    key={section.resource}
                    onClick={() => onOpenChild(section.resource)}
                  >
                    <Icon
                      name={iconForResource(section.resource, section.title)}
                      size={15}
                    />
                    {section.title} · {section.items.length}
                  </button>
                ))}
              </div>
            )}
            <button className="sol-btn" onClick={onBack}>
              Все {resource.title.toLowerCase()}
            </button>
          </div>
        </>
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
        <div className="sol-block-h">{section.title}</div>
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
      <div className="sol-block-h">{section.title}</div>
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
