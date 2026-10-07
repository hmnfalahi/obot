// Typing and wrappers for the parts of Telegram.WebApp we use; the only module that touches it.
// https://core.telegram.org/bots/webapps#initializing-mini-apps
import { createBottomButtons, type BottomButtonOptions, type Slot } from './bottomButtons'

interface BackButton {
  show(): void
  hide(): void
  onClick(cb: () => void): void
  offClick(cb: () => void): void
}

interface BottomButton {
  setParams(params: { text?: string; is_active?: boolean; has_shine_effect?: boolean }): void
  show(): void
  hide(): void
  showProgress(leaveActive?: boolean): void
  hideProgress(): void
  onClick(cb: () => void): void
  offClick(cb: () => void): void
}

interface PopupButton {
  id?: string
  type?: 'default' | 'ok' | 'close' | 'cancel' | 'destructive'
  text?: string
}

interface CloudStorage {
  getItem(key: string, cb: (error: string | null, value?: string) => void): void
  setItem(key: string, value: string, cb?: (error: string | null) => void): void
  removeItem(key: string, cb?: (error: string | null) => void): void
}

type ThemeColorKey = 'bg_color' | 'secondary_bg_color'

interface WebApp {
  initData: string
  initDataUnsafe: { start_param?: string; user?: { language_code?: string } }
  version: string
  colorScheme: 'light' | 'dark'
  isVersionAtLeast(version: string): boolean
  ready(): void
  expand(): void
  setHeaderColor(color: ThemeColorKey): void
  setBackgroundColor(color: ThemeColorKey): void
  setBottomBarColor(color: ThemeColorKey): void
  enableClosingConfirmation(): void
  disableClosingConfirmation(): void
  enableVerticalSwipes(): void
  disableVerticalSwipes(): void
  openTelegramLink(url: string): void
  openInvoice(url: string, callback?: (status: 'paid' | 'cancelled' | 'failed' | 'pending') => void): void
  showAlert(message: string): void
  showConfirm(message: string, callback: (ok: boolean) => void): void
  showPopup(params: { title?: string; message: string; buttons?: PopupButton[] }, callback?: (id: string) => void): void
  BackButton: BackButton
  MainButton: BottomButton
  SecondaryButton?: BottomButton
  SettingsButton?: BackButton
  CloudStorage?: CloudStorage
  HapticFeedback?: {
    notificationOccurred(type: 'success' | 'error' | 'warning'): void
    impactOccurred(style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft'): void
    selectionChanged(): void
  }
}

declare global {
  interface Window {
    Telegram?: { WebApp: WebApp }
  }
}

export const webApp: WebApp | undefined = window.Telegram?.WebApp

/** True inside a Telegram client; false in a plain browser (development). */
export const insideTelegram = Boolean(webApp?.initData)

/** Whether the Telegram client supports a Bot API (Mini Apps) version; false outside Telegram. */
export function supports(version: string): boolean {
  return insideTelegram && webApp!.isVersionAtLeast(version)
}

/** Called once at startup: tell Telegram we are ready and match its chrome to our pages. */
export function initTelegram(): void {
  if (!webApp) return
  webApp.ready()
  webApp.expand()
  if (supports('6.1')) {
    webApp.setHeaderColor('secondary_bg_color')
    webApp.setBackgroundColor('secondary_bg_color')
  }
  if (supports('7.10')) webApp.setBottomBarColor('secondary_bg_color')
}

/** Signed initData from Telegram, or a dev-only signed value outside Telegram. */
export function initData(): string {
  if (webApp?.initData) return webApp.initData
  return import.meta.env.DEV ? (import.meta.env.VITE_DEV_INIT_DATA ?? '') : ''
}

export function startParam(): string | undefined {
  return webApp?.initDataUnsafe.start_param
}

export function openTelegramLink(url: string): void {
  if (insideTelegram) webApp!.openTelegramLink(url)
  else window.open(url, '_blank')
}

/** Telegram's yes/no dialog; with `action` the confirm button is named (red if destructive). */
export function confirm(message: string, opts: { action?: string; destructive?: boolean } = {}): Promise<boolean> {
  if (opts.action && supports('6.2'))
    return new Promise((resolve) =>
      webApp!.showPopup(
        {
          message,
          buttons: [
            { id: 'ok', type: opts.destructive ? 'destructive' : 'default', text: opts.action },
            { id: 'cancel', type: 'cancel' },
          ],
        },
        (id) => resolve(id === 'ok'),
      ),
    )
  if (supports('6.2')) return new Promise((resolve) => webApp!.showConfirm(message, resolve))
  return Promise.resolve(window.confirm(message))
}

export function haptic(type: 'success' | 'error'): void {
  if (supports('6.1')) webApp?.HapticFeedback?.notificationOccurred(type)
}

export function selectionHaptic(): void {
  if (supports('6.1')) webApp?.HapticFeedback?.selectionChanged()
}

export function impactHaptic(style: 'light' | 'medium' | 'heavy' = 'light'): void {
  if (supports('6.1')) webApp?.HapticFeedback?.impactOccurred(style)
}

/** Opens Telegram's Stars payment sheet; resolves with its final status. */
export function openInvoice(url: string): Promise<string> {
  return new Promise((resolve) => {
    if (insideTelegram) webApp!.openInvoice(url, resolve)
    else {
      window.open(url, '_blank')
      resolve('pending')
    }
  })
}

// --- storage: Telegram CloudStorage (synced across the user's devices), localStorage outside it

const LOCAL_PREFIX = 'obot:'

export function cloudGet(key: string): Promise<string | null> {
  if (supports('6.9') && webApp!.CloudStorage)
    return new Promise((resolve) => webApp!.CloudStorage!.getItem(key, (error, value) => resolve(error ? null : value || null)))
  try {
    return Promise.resolve(localStorage.getItem(LOCAL_PREFIX + key))
  } catch {
    return Promise.resolve(null)
  }
}

export function cloudSet(key: string, value: string): Promise<void> {
  if (supports('6.9') && webApp!.CloudStorage)
    return new Promise((resolve) => webApp!.CloudStorage!.setItem(key, value, () => resolve()))
  try {
    localStorage.setItem(LOCAL_PREFIX + key, value)
  } catch {
    // storage unavailable (private window); nothing to remember
  }
  return Promise.resolve()
}

export function cloudRemove(key: string): Promise<void> {
  if (supports('6.9') && webApp!.CloudStorage)
    return new Promise((resolve) => webApp!.CloudStorage!.removeItem(key, () => resolve()))
  try {
    localStorage.removeItem(LOCAL_PREFIX + key)
  } catch {
    // storage unavailable
  }
  return Promise.resolve()
}

// --- Telegram chrome

/** Shows the "Settings" item in the Mini App's menu; returns a function that removes it. */
export function onSettingsButton(cb: () => void): () => void {
  const button = supports('7.0') ? webApp!.SettingsButton : undefined
  if (!button) return () => {}
  button.onClick(cb)
  button.show()
  return () => {
    button.offClick(cb)
    button.hide()
  }
}

/** While a form has unsaved changes: ask before closing, and do not close on a downward swipe. */
export function setClosingGuard(on: boolean): void {
  if (!supports('6.2')) return
  if (on) webApp!.enableClosingConfirmation()
  else webApp!.disableClosingConfirmation()
  if (supports('7.7')) {
    if (on) webApp!.disableVerticalSwipes()
    else webApp!.enableVerticalSwipes()
  }
}

// --- bottom buttons: Telegram's MainButton/SecondaryButton, or the in-page fallback outside it

function telegramButton(slot: Slot): BottomButton | undefined {
  if (!insideTelegram) return undefined
  if (slot === 'main') return webApp!.MainButton
  return supports('7.10') ? webApp!.SecondaryButton : undefined
}

export const bottomButtons = createBottomButtons({
  show(slot: Slot, o: BottomButtonOptions) {
    const button = telegramButton(slot)
    if (!button) return
    button.setParams({
      text: o.text,
      is_active: !o.disabled && !o.loading,
      ...(supports('7.10') ? { has_shine_effect: Boolean(o.shine) } : {}),
    })
    if (o.loading) button.showProgress(false)
    else button.hideProgress()
    button.show()
  },
  hide(slot: Slot) {
    const button = telegramButton(slot)
    if (!button) return
    button.hideProgress()
    button.hide()
  },
})

const slotHandlers = {
  main: () => bottomButtons.click('main'),
  secondary: () => bottomButtons.click('secondary'),
}
for (const slot of ['main', 'secondary'] as const) telegramButton(slot)?.onClick(slotHandlers[slot])
// Vite hot reload re-runs this module; drop the old handlers so clicks are not delivered twice.
import.meta.hot?.dispose(() => {
  for (const slot of ['main', 'secondary'] as const) telegramButton(slot)?.offClick(slotHandlers[slot])
})

/** Whether bottom buttons must be drawn in the page (outside Telegram, or no SecondaryButton). */
export function needsFallback(slot: Slot): boolean {
  return telegramButton(slot) === undefined
}
