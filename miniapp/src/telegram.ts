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
  openInvoice(url: string, callback?: (status: 'paid' | 'cancelled' | 'failed' | 'pending') => void): void
  showAlert(message: string): void
  showConfirm(message: string, callback: (ok: boolean) => void): void
  downloadFile?(params: { url: string; file_name: string }, callback?: (accepted: boolean) => void): void
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

/** Telegram's own yes/no dialog; the browser's outside Telegram. */
export function confirm(message: string): Promise<boolean> {
  if (webApp?.initData) return new Promise((resolve) => webApp!.showConfirm(message, resolve))
  return Promise.resolve(window.confirm(message))
}

export function haptic(type: 'success' | 'error'): void {
  webApp?.HapticFeedback?.notificationOccurred(type)
}

/** Opens Telegram's Stars payment sheet; resolves with its final status. */
export function openInvoice(url: string): Promise<string> {
  return new Promise((resolve) => {
    if (webApp?.initData) webApp.openInvoice(url, resolve)
    else {
      window.open(url, '_blank')
      resolve('pending')
    }
  })
}

/** Telegram's download sheet for a file at `url`; a new browser tab outside Telegram. */
export function downloadFile(url: string, fileName: string): void {
  if (webApp?.initData && webApp.downloadFile) webApp.downloadFile({ url, file_name: fileName })
  else window.open(url, '_blank')
}
