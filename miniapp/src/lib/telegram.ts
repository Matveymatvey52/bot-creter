/* Optional Telegram Mini App layer — see docs/MINIAPP_DESIGN.md §2.1: the
   SAME build serves both the Telegram WebView and a plain browser tab, so
   every access to window.Telegram.WebApp must degrade gracefully when it's
   absent (plain browser) rather than assume it exists. */

interface TelegramWebAppUser {
  id: number
  photo_url?: string
}

interface TelegramWebApp {
  initData: string
  initDataUnsafe?: { user?: TelegramWebAppUser }
  ready: () => void
  expand: () => void
  MainButton: {
    text: string
    show: () => void
    hide: () => void
    onClick: (cb: () => void) => void
    offClick: (cb: () => void) => void
    setParams: (params: { text?: string; is_active?: boolean }) => void
  }
  HapticFeedback?: {
    impactOccurred: (style: 'light' | 'medium' | 'heavy') => void
  }
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

export function getTelegramWebApp(): TelegramWebApp | null {
  return window.Telegram?.WebApp ?? null
}

export function getInitData(): string | null {
  const webApp = getTelegramWebApp()
  return webApp?.initData || null
}

/** True only when initData is actually present — a bare window.Telegram.WebApp
    object with no initData (some in-app browsers inject a stub) is treated
    as "not really in Telegram" for auth purposes, since miniapp_api.py's
    _authenticate() would reject an empty initData header the same way. */
export function isInTelegram(): boolean {
  return Boolean(getInitData())
}

/** Доступна ли нативная кнопка Telegram. Единственный источник правды для
    правила «одна кнопка отправки»: если она есть, экран НЕ рисует свою — иначе
    внутри Telegram видно две кнопки сразу, медную в теле формы и синюю
    системную снизу. Вне Telegram (десктоп-браузер, страница /site/{bot_id})
    её нет, и свою кнопку рисовать обязательно — иначе отправлять будет
    нечем. */
export function hasMainButton(): boolean {
  return isInTelegram() && Boolean(getTelegramWebApp()?.MainButton)
}

/** The Telegram WebApp's own `initDataUnsafe.user.photo_url` field (see
    Telegram's Mini Apps docs) — undefined outside Telegram or when Telegram
    hasn't handed one back (e.g. the user has no profile photo). Deliberately
    NOT verified against initData's HMAC signature (initDataUnsafe, as the
    name says, isn't) — fine here since it's only ever used for a decorative
    avatar image, never for auth. Standalone-site magic-link sessions
    (docs/MINIAPP_WEBSITE_REDESIGN_DESIGN.md Task B) don't carry a Telegram
    photo today — see mint_magic_link_token()/mint_site_link_token() in
    runtime/miniapp_api.py, neither embeds one — so ScreenHeader simply
    renders without an avatar there. */
export function getTelegramUserPhotoUrl(): string | undefined {
  return getTelegramWebApp()?.initDataUnsafe?.user?.photo_url
}
