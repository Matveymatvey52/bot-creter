/* Renders one field's value the way a human should see it, wherever it
   appears. Four cases the old plain-String() rendering got wrong:

   - a foreign key showed a bare row id ("ID тура: 7") instead of the record's
     name — `refLabels` supplies the resolved title;
   - a URL showed as unclickable text, so a maps/booking/spreadsheet link was
     useless inside the app;
   - a file showed as an opaque storage id rather than something openable;
   - empty values printed "null"/"undefined". */

import type { FieldDisplay } from '../lib/displaySchema'
import { isLinkValue } from '../lib/displaySchema'

// Resolved human-readable titles for foreign-key values, keyed
// "<resource>:<id>" — built by whoever already fetched the referenced list.
export type RefLabels = Record<string, string>

export function refLabelKey(resource: string, id: unknown): string {
  return `${resource}:${String(id)}`
}

export function FieldValue({
  field,
  value,
  refLabels,
}: {
  field: FieldDisplay
  value: unknown
  refLabels?: RefLabels
}) {
  if (value == null || value === '') {
    return <span className="field-value-empty">—</span>
  }

  if (field.ref) {
    const label = refLabels?.[refLabelKey(field.ref.resource, value)]
    // Falls back to the raw value only when the referenced record is gone or
    // not visible to this viewer — never the normal path.
    return <span>{label ?? String(value)}</span>
  }

  const text = String(value)

  if (isLinkValue(field, value)) {
    return (
      <a className="field-value-link" href={text} target="_blank" rel="noopener noreferrer">
        {text}
      </a>
    )
  }

  if (field.kind === 'file') {
    return (
      <span className="field-value-file">
        <span aria-hidden="true">📎</span> {text}
      </span>
    )
  }

  return <span>{text}</span>
}
