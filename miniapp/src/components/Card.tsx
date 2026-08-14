import type { ReactNode } from 'react'

export function Card({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return (
    <div className="card" onClick={onClick} role={onClick ? 'button' : undefined}>
      {children}
    </div>
  )
}

export function CardHeader({ children }: { children: ReactNode }) {
  return <div className="card-header">{children}</div>
}

export function CardTitle({ children }: { children: ReactNode }) {
  return <div className="card-title">{children}</div>
}

export function ChipRow({ children }: { children: ReactNode }) {
  return <div className="chip-row">{children}</div>
}

export function Chip({ children }: { children: ReactNode }) {
  return <span className="chip">{children}</span>
}

export function Badge({ tone = 'neutral', children }: { tone?: 'success' | 'neutral'; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}
