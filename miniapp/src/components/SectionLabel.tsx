import type { ReactNode } from 'react'

/* Small caps label above a content block ("ИЛИ ВЫБЕРИТЕ ТАРИФ") — trivial
   wrapper over --font-label-caps, per docs/MINIAPP_WEBSITE_REDESIGN_DESIGN.md §1.3. */

export function SectionLabel({ children }: { children: ReactNode }) {
  return <p className="label-caps section-label">{children}</p>
}
