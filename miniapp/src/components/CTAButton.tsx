import type { ReactNode } from 'react'

/* Compact, rectangular accent CTA — replaces the full-width oval
   .btn-primary/.btn-secondary classes for the redesign's main-action row.
   Exact sizing per docs/miniapp_redesign_examples.html: border-radius 10px,
   padding 11px 16px, font 600 14px, icon + label inline, auto-width. */

export function CTAButton({
  icon,
  variant,
  onClick,
  disabled,
  children,
}: {
  icon?: ReactNode
  variant: 'primary' | 'secondary'
  onClick?: () => void
  disabled?: boolean
  children: ReactNode
}) {
  return (
    <button className={`cta cta-${variant}`} onClick={onClick} disabled={disabled}>
      {icon && <span className="cta-icon">{icon}</span>}
      {children}
    </button>
  )
}
