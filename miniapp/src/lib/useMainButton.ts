import { useEffect } from 'react'
import { getTelegramWebApp, isInTelegram } from './telegram'

/** The one real UI branch between Telegram WebView and plain browser (see
    docs/MINIAPP_DESIGN.md §2.1): inside Telegram, the primary action uses
    the native MainButton (bottom of the WebView chrome, not part of page
    layout); in a browser there's no such API, so the caller renders its own
    fixed .bottom-bar button instead. `active` lets a screen disable/hide
    the button conditionally (e.g. a create form for a bot with no fields
    filled in yet) without unmounting. */
export function useTelegramMainButton(text: string, onClick: () => void, active: boolean) {
  useEffect(() => {
    if (!isInTelegram()) return
    const webApp = getTelegramWebApp()
    if (!webApp) return

    webApp.MainButton.setParams({ text, is_active: active })
    if (active) {
      webApp.MainButton.show()
    } else {
      webApp.MainButton.hide()
    }
    webApp.MainButton.onClick(onClick)
    return () => {
      webApp.MainButton.offClick(onClick)
      webApp.MainButton.hide()
    }
  }, [text, onClick, active])
}
