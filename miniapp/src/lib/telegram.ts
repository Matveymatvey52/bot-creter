/* Optional Telegram Mini App layer — see docs/MINIAPP_DESIGN.md §2.1: the
   SAME build serves both the Telegram WebView and a plain browser tab, so
   every access to window.Telegram.WebApp must degrade gracefully when it's
   absent (plain browser) rather than assume it exists. */

interface TelegramWebApp {
  initData: string
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
