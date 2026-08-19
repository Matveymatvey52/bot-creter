/* Big two-line screen header — replaces the plain <h1> generic screens used
   to render. Per docs/MINIAPP_WEBSITE_REDESIGN_DESIGN.md §1.3: eyebrow label
   + display-lg title, optional circular avatar with a Telegram badge overlay
   when the photo came from Telegram. No avatarUrl → no avatar slot at all —
   the owner explicitly didn't want a decorative fallback icon standing in
   for a real photo. */

export function ScreenHeader({
  eyebrow,
  title,
  avatarUrl,
}: {
  eyebrow: string
  title: string
  avatarUrl?: string
}) {
  return (
    <div className="screen-header-v2">
      <div className="screen-header-v2-titles">
        <span className="label-caps">{eyebrow}</span>
        <h1 className="display-lg">{title}</h1>
      </div>
      {avatarUrl && (
        <div className="avatar">
          <img src={avatarUrl} alt="" />
          <div className="tg-badge">
            <svg viewBox="0 0 24 24" fill="#fff">
              <path d="M21 4L2 11l6 2.5L20 6l-9.5 9L20 19l1-15z" />
            </svg>
          </div>
        </div>
      )}
    </div>
  )
}
