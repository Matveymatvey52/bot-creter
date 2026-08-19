/* Tariff/option picker card — active state gets an accent border + glow
   (--shadow-card-active), per docs/miniapp_redesign_examples.html's .tariff/
   .tariff.active. Built as a standalone component (not layered on the
   existing Card primitive) since its internal layout — name/meta on the
   left, price stack on the right — doesn't match Card's header+body shape. */

export function SelectableCard({
  active,
  title,
  meta,
  oldPrice,
  newPrice,
  discountLabel,
  onClick,
}: {
  active?: boolean
  title: string
  meta?: string
  oldPrice?: string
  newPrice: string
  discountLabel?: string
  onClick?: () => void
}) {
  return (
    <div
      className={`tariff${active ? ' tariff-active' : ''}`}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
    >
      <div>
        <div className="tariff-name">{title}</div>
        {meta && <div className="tariff-meta">{meta}</div>}
      </div>
      <div className="tariff-price">
        {oldPrice && <span className="old-price">{oldPrice}</span>}
        <span className="new-price">{newPrice}</span>
        {discountLabel && <span className="badge badge-success discount">{discountLabel}</span>}
      </div>
    </div>
  )
}
