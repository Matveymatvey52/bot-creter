/* Horizontal, swipeable section navigation — replaces the wrapping row of
   pill buttons the app used to render for miniapp_config's resources. Two
   behaviours the old row lacked: it never reflows onto a second/third line
   (a bot with 8 resources ate a third of the screen before any content), and
   the active section scrolls itself into view when the route changes from
   somewhere other than a tap on the rail itself. */

import { useEffect, useRef } from 'react'

export interface RailItem {
  key: string
  label: string
}

export function SectionRail({
  items,
  activeKey,
  onSelect,
}: {
  items: RailItem[]
  activeKey: string | null
  onSelect: (key: string) => void
}) {
  const railRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const active = railRef.current?.querySelector<HTMLElement>('[data-active="true"]')
    // 'nearest' keeps a rail that already fits from jumping the page around;
    // only an off-screen tab actually scrolls.
    active?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' })
  }, [activeKey])

  return (
    <div className="section-rail" ref={railRef} role="tablist">
      {items.map((item) => {
        const isActive = item.key === activeKey
        return (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={isActive}
            data-active={isActive}
            className={isActive ? 'section-rail-item section-rail-item-active' : 'section-rail-item'}
            onClick={() => onSelect(item.key)}
          >
            {item.label}
          </button>
        )
      })}
    </div>
  )
}
