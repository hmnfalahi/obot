// Minimal typing for the parts of Telegram.WebApp we use.
// https://core.telegram.org/bots/webapps#initializing-mini-apps

interface BackButton {
  show(): void
  hide(): void
  onClick(cb: () => void): void
  offClick(cb: () => void): void
}

interface WebApp {
  initData: string
  initDataUnsafe: { start_param?: string; user?: { language_code?: string } }
  colorScheme: 'light' | 'dark'
  ready(): void
  expand(): void
  openTelegramLink(url: string): void
  showAlert(message: string): void
  BackButton: BackButton
  HapticFeedback?: { notificationOccurred(type: 'success' | 'error' | 'warning'): void }
}

declare global {
  interface Window {
    Telegram?: { WebApp: WebApp }
  }
}

export const webApp: WebApp | undefined = window.Telegram?.WebApp

/** Signed initData from Telegram, or a dev-only signed value outside Telegram. */
export function initData(): string {
  if (webApp?.initData) return webApp.initData
  return import.meta.env.DEV ? (import.meta.env.VITE_DEV_INIT_DATA ?? '') : ''
}

export function startParam(): string | undefined {
  return webApp?.initDataUnsafe.start_param
}

export function openTelegramLink(url: string): void {
  if (webApp?.initData) webApp.openTelegramLink(url)
  else window.open(url, '_blank')
}

export function haptic(type: 'success' | 'error'): void {
  webApp?.HapticFeedback?.notificationOccurred(type)
}
