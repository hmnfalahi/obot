# UX-1: Mini App Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the mini app a native Telegram shell — bottom tab bar, a stack router driven by Telegram's BackButton, Telegram's MainButton/SecondaryButton for primary actions, popups, theme-matched chrome — while the existing pages keep working inside it.

**Architecture:** Pure logic (navigation reducer, bottom-button store) lives in small TypeScript modules with no React/DOM imports so Node's built-in test runner can test them. `telegram.ts` stays the only place that touches `window.Telegram.WebApp`, and every newer method is gated with `isVersionAtLeast`. `App.tsx` becomes a shell: it owns the navigation stack, renders the top route plus a `TabBar` on tab roots, and wires BackButton/SettingsButton/CloudStorage.

**Tech Stack:** React 19, TypeScript 6, Vite 8, oxlint; tests with `node --test` (Node 22.18+ strips TypeScript types natively, no new dependency).

**Spec:** `docs/superpowers/specs/2026-10-07-panel-ux-design.md` (section UX-1)

## Global Constraints

- No new runtime or dev dependencies in `miniapp/package.json`.
- Colors only from Telegram theme variables (`--tg-theme-*`) with the existing fallbacks in `index.css`.
- Full RTL support: use logical CSS properties (`inset-inline-*`, `padding-inline-*`, `text-align: start`), never `left`/`right`.
- Every Telegram method newer than 6.0 is called only after `webApp.isVersionAtLeast('<version>')`; older clients keep today's behaviour.
- Outside Telegram (`initData` empty, dev browser) nothing throws; bottom buttons render as an in-page fallback bar.
- Existing deep links keep working: `?bot=<id>`, `?mine=<id>`, `start_param=bot_<id>`.
- UI strings go through `i18n.ts` with both `en` and `fa` entries.

## Scope notes (deviations from the spec, deliberate)

- The spec lists the whole UI kit (`Section`, `Cell`, `Switch`, `Field`, `Select`, `Avatar`, `Badge`, `Skeleton`, `EmptyState`, `Banner`, `Toast`) under UX-1. They are built in UX-2's first task, next to the pages that first use them, so no component is written without a consumer. UX-1 builds only `TabBar` and the bottom-button fallback.
- `addToHomeScreen`/`checkHomeScreenStatus` (used by the settings tab) and `requestChat` (UX-3) are added to `telegram.ts` in the tasks that consume them.
- `disableVerticalSwipes` is tied to "form has unsaved changes" (same hook as closing confirmation) instead of input focus: it protects the same thing with one signal.
- The credits tab gets today's credit/purchase/wallet blocks moved verbatim from `SettingsPage` into a new `CreditsPage`; its redesign is UX-2.

## File Structure

| File | Responsibility |
|---|---|
| `miniapp/src/nav.ts` (create) | Route/Tab types, pure navigation reducer, initial stack from URL |
| `miniapp/src/bottomButtons.ts` (create) | Pure store for MainButton/SecondaryButton state with an injectable adapter |
| `miniapp/src/telegram.ts` (modify) | Full WebApp typing; `initTelegram`, `supports`, popup confirm, CloudStorage, haptics, SettingsButton, closing guard, Telegram bottom-button adapter |
| `miniapp/src/telegramHooks.tsx` (create) | React hooks `useMainButton`, `useSecondaryButton`, `useDirtyGuard`; `<BottomButtonsFallback/>` |
| `miniapp/src/ui/TabBar.tsx` (create) | Bottom tab bar |
| `miniapp/src/ui.tsx` → `miniapp/src/ui/index.tsx` (move) | Existing `Loading`/`ErrorBox`, plus re-export of `TabBar` |
| `miniapp/src/App.tsx` (modify) | Shell: stack, tabs, BackButton, SettingsButton, last-tab memory |
| `miniapp/src/pages/CreditsPage.tsx` (create) | Credit stats, Stars packages, wallet (moved from SettingsPage) |
| `miniapp/src/pages/SettingsPage.tsx` (modify) | Language and timezone only |
| `miniapp/src/pages/BotsPage.tsx`, `NewBotPage.tsx`, `MySettingsPage.tsx` (modify) | Use MainButton and the new navigation |
| `miniapp/src/i18n.ts` (modify) | Tab labels |
| `miniapp/src/index.css` (modify) | Tab bar, fallback bar, safe-area padding |
| `miniapp/tests/nav.test.ts`, `miniapp/tests/bottomButtons.test.ts` (create) | Unit tests |
| `miniapp/package.json` (modify) | `test` script |

Tests live in `miniapp/tests/`, outside `src/`, so `tsconfig.app.json` (which has only `vite/client` types) never sees `node:test` imports.

---

### Task 1: Navigation reducer with tests

**Files:**
- Create: `miniapp/src/nav.ts`
- Create: `miniapp/tests/nav.test.ts`
- Modify: `miniapp/package.json` (scripts)

**Interfaces:**
- Produces (used by Task 4 and every page):
  ```ts
  export type Tab = 'bots' | 'tasks' | 'credits' | 'settings'
  export const TABS: Tab[]
  export type Route =
    | { page: Tab }
    | { page: 'bot'; id: number }
    | { page: 'mine'; id: number }
    | { page: 'new' }
  export type Stack = Route[]               // never empty; stack[0] is a tab root
  export type NavAction =
    | { type: 'push'; route: Route }        // pushing a tab root switches to that tab
    | { type: 'back' }
    | { type: 'replace'; route: Route }
    | { type: 'tab'; tab: Tab }
  export function isTab(page: string): page is Tab
  export function reduce(stack: Stack, action: NavAction): Stack
  export function currentTab(stack: Stack): Tab
  export function initialStack(search: string, startParam: string | undefined): Stack
  ```

- [ ] **Step 1: Add the test script**

In `miniapp/package.json` add to `"scripts"` (after `"lint"`):

```json
    "test": "node --test 'tests/**/*.test.ts'",
```

- [ ] **Step 2: Write the failing tests**

Create `miniapp/tests/nav.test.ts`:

```ts
import assert from 'node:assert/strict'
import test from 'node:test'
import { currentTab, initialStack, reduce, type Stack } from '../src/nav.ts'

const home: Stack = [{ page: 'bots' }]

test('push adds a page on top', () => {
  assert.deepEqual(reduce(home, { type: 'push', route: { page: 'bot', id: 3 } }), [
    { page: 'bots' },
    { page: 'bot', id: 3 },
  ])
})

test('pushing a tab root switches tabs instead of stacking', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'mine', id: 2 }]
  assert.deepEqual(reduce(deep, { type: 'push', route: { page: 'credits' } }), [{ page: 'credits' }])
})

test('back pops one page and never empties the stack', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'bot', id: 3 }, { page: 'mine', id: 3 }]
  assert.deepEqual(reduce(deep, { type: 'back' }), [{ page: 'bots' }, { page: 'bot', id: 3 }])
  assert.deepEqual(reduce(home, { type: 'back' }), home)
})

test('replace swaps the top page', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'new' }]
  assert.deepEqual(reduce(deep, { type: 'replace', route: { page: 'bot', id: 9 } }), [
    { page: 'bots' },
    { page: 'bot', id: 9 },
  ])
})

test('replace on a tab root keeps a tab root', () => {
  assert.deepEqual(reduce(home, { type: 'replace', route: { page: 'bot', id: 1 } }), [
    { page: 'bots' },
    { page: 'bot', id: 1 },
  ])
})

test('tab resets to that tab root', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'bot', id: 3 }]
  assert.deepEqual(reduce(deep, { type: 'tab', tab: 'tasks' }), [{ page: 'tasks' }])
})

test('currentTab is the root of the stack', () => {
  assert.equal(currentTab([{ page: 'settings' }]), 'settings')
  assert.equal(currentTab([{ page: 'bots' }, { page: 'new' }]), 'bots')
})

test('initialStack honours the existing deep links', () => {
  assert.deepEqual(initialStack('', undefined), home)
  assert.deepEqual(initialStack('?bot=4', undefined), [{ page: 'bots' }, { page: 'bot', id: 4 }])
  assert.deepEqual(initialStack('?mine=5', undefined), [{ page: 'bots' }, { page: 'mine', id: 5 }])
  assert.deepEqual(initialStack('', 'bot_6'), [{ page: 'bots' }, { page: 'bot', id: 6 }])
  assert.deepEqual(initialStack('?bot=abc', 'nonsense'), home)
})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd miniapp && npm test`
Expected: FAIL — `Cannot find module '.../src/nav.ts'`.

- [ ] **Step 4: Implement `nav.ts`**

Create `miniapp/src/nav.ts`:

```ts
/** Navigation as a stack of routes; the bottom of the stack is always a tab root. */

export type Tab = 'bots' | 'tasks' | 'credits' | 'settings'
export const TABS: Tab[] = ['bots', 'tasks', 'credits', 'settings']

export type Route =
  | { page: Tab }
  | { page: 'bot'; id: number }
  | { page: 'mine'; id: number }
  | { page: 'new' }

export type Stack = Route[]

export type NavAction =
  | { type: 'push'; route: Route }
  | { type: 'back' }
  | { type: 'replace'; route: Route }
  | { type: 'tab'; tab: Tab }

export function isTab(page: string): page is Tab {
  return (TABS as string[]).includes(page)
}

export function reduce(stack: Stack, action: NavAction): Stack {
  switch (action.type) {
    case 'push':
      return isTab(action.route.page) ? [action.route] : [...stack, action.route]
    case 'back':
      return stack.length > 1 ? stack.slice(0, -1) : stack
    case 'replace':
      if (isTab(action.route.page)) return [action.route]
      return stack.length > 1 ? [...stack.slice(0, -1), action.route] : [...stack, action.route]
    case 'tab':
      return [{ page: action.tab }]
  }
}

export function currentTab(stack: Stack): Tab {
  return stack[0].page as Tab
}

/** The factory opens one bot with ?bot=<id> (web_app button) or start_param "bot_<id>" (link),
 * and someone's own settings for a bot with ?mine=<id>. */
export function initialStack(search: string, startParam: string | undefined): Stack {
  const params = new URLSearchParams(search)
  const home: Route = { page: 'bots' }
  const mine = Number(params.get('mine'))
  if (mine > 0) return [home, { page: 'mine', id: mine }]
  const fromStart = /^bot_(\d+)$/.exec(startParam ?? '')?.[1]
  const id = Number(params.get('bot') ?? fromStart)
  return id > 0 ? [home, { page: 'bot', id }] : [home]
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd miniapp && npm test`
Expected: PASS, 8 tests.

---

### Task 2: Bottom-button store with tests

**Files:**
- Create: `miniapp/src/bottomButtons.ts`
- Create: `miniapp/tests/bottomButtons.test.ts`

**Interfaces:**
- Produces (used by Task 3):
  ```ts
  export type Slot = 'main' | 'secondary'
  export interface BottomButtonOptions {
    text: string
    onClick: () => void
    disabled?: boolean   // greyed out, not clickable
    loading?: boolean    // spinner, not clickable
    shine?: boolean      // Telegram's shine effect
  }
  export interface BottomButtonAdapter {
    show(slot: Slot, options: BottomButtonOptions): void
    hide(slot: Slot): void
  }
  export interface BottomButtons {
    set(slot: Slot, options: BottomButtonOptions | null): void
    get(slot: Slot): BottomButtonOptions | null
    click(slot: Slot): void              // calls the latest onClick if clickable
    subscribe(listener: () => void): () => void
  }
  export function createBottomButtons(adapter: BottomButtonAdapter): BottomButtons
  ```

- [ ] **Step 1: Write the failing tests**

Create `miniapp/tests/bottomButtons.test.ts`:

```ts
import assert from 'node:assert/strict'
import test from 'node:test'
import { createBottomButtons, type BottomButtonAdapter, type Slot } from '../src/bottomButtons.ts'

function recorder() {
  const calls: string[] = []
  const adapter: BottomButtonAdapter = {
    show: (slot: Slot, o) => calls.push(`show ${slot} ${o.text}${o.disabled ? ' disabled' : ''}${o.loading ? ' loading' : ''}`),
    hide: (slot: Slot) => calls.push(`hide ${slot}`),
  }
  return { calls, adapter }
}

test('set shows the button and null hides it', () => {
  const { calls, adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  buttons.set('main', { text: 'Save', onClick: () => {} })
  buttons.set('main', null)
  assert.deepEqual(calls, ['show main Save', 'hide main'])
  assert.equal(buttons.get('main'), null)
})

test('unchanged options do not touch Telegram again', () => {
  const { calls, adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  buttons.set('main', { text: 'Save', onClick: () => {} })
  buttons.set('main', { text: 'Save', onClick: () => {} })
  buttons.set('main', { text: 'Save', onClick: () => {}, loading: true })
  assert.deepEqual(calls, ['show main Save', 'show main Save loading'])
})

test('click runs the latest handler', () => {
  const { adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  const hits: string[] = []
  buttons.set('secondary', { text: 'Back', onClick: () => hits.push('old') })
  buttons.set('secondary', { text: 'Back', onClick: () => hits.push('new') })
  buttons.click('secondary')
  assert.deepEqual(hits, ['new'])
})

test('click is ignored while disabled, loading or hidden', () => {
  const { adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  let hits = 0
  buttons.set('main', { text: 'Go', onClick: () => hits++, disabled: true })
  buttons.click('main')
  buttons.set('main', { text: 'Go', onClick: () => hits++, loading: true })
  buttons.click('main')
  buttons.set('main', null)
  buttons.click('main')
  assert.equal(hits, 0)
})

test('subscribers hear every change and can leave', () => {
  const { adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  let heard = 0
  const leave = buttons.subscribe(() => heard++)
  buttons.set('main', { text: 'A', onClick: () => {} })
  leave()
  buttons.set('main', null)
  assert.equal(heard, 1)
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd miniapp && npm test`
Expected: FAIL — `Cannot find module '.../src/bottomButtons.ts'`.

- [ ] **Step 3: Implement `bottomButtons.ts`**

Create `miniapp/src/bottomButtons.ts`:

```ts
/** State of Telegram's two bottom buttons, kept outside React so one page owns each slot at a time. */

export type Slot = 'main' | 'secondary'

export interface BottomButtonOptions {
  text: string
  onClick: () => void
  disabled?: boolean
  loading?: boolean
  shine?: boolean
}

/** What actually draws the buttons: Telegram's MainButton/SecondaryButton, or nothing in tests. */
export interface BottomButtonAdapter {
  show(slot: Slot, options: BottomButtonOptions): void
  hide(slot: Slot): void
}

export interface BottomButtons {
  set(slot: Slot, options: BottomButtonOptions | null): void
  get(slot: Slot): BottomButtonOptions | null
  click(slot: Slot): void
  subscribe(listener: () => void): () => void
}

const looks = (o: BottomButtonOptions | null) =>
  o && `${o.text}|${o.disabled ?? false}|${o.loading ?? false}|${o.shine ?? false}`

export function createBottomButtons(adapter: BottomButtonAdapter): BottomButtons {
  const state: Record<Slot, BottomButtonOptions | null> = { main: null, secondary: null }
  const listeners = new Set<() => void>()

  return {
    set(slot, options) {
      const changed = looks(state[slot]) !== looks(options)
      state[slot] = options
      if (changed) {
        if (options) adapter.show(slot, options)
        else adapter.hide(slot)
      }
      listeners.forEach((listener) => listener())
    },
    get: (slot) => state[slot],
    click(slot) {
      const options = state[slot]
      if (options && !options.disabled && !options.loading) options.onClick()
    },
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd miniapp && npm test`
Expected: PASS, 13 tests.

---

### Task 3: Telegram wrapper and React hooks

**Files:**
- Modify: `miniapp/src/telegram.ts` (whole file)
- Create: `miniapp/src/telegramHooks.tsx`
- Modify: `miniapp/src/main.tsx`
- Modify: `miniapp/src/index.css` (fallback bar, safe area)

**Interfaces:**
- Consumes: `createBottomButtons`, `BottomButtonOptions`, `Slot` from Task 2.
- Produces:
  ```ts
  // telegram.ts (existing exports keep their signatures: webApp, initData, startParam,
  // openTelegramLink, haptic, openInvoice; confirm gains an optional second argument)
  export const insideTelegram: boolean
  export function supports(version: string): boolean
  export function initTelegram(): void
  export function confirm(message: string, opts?: { action?: string; destructive?: boolean }): Promise<boolean>
  export function selectionHaptic(): void
  export function impactHaptic(style?: 'light' | 'medium' | 'heavy'): void
  export function cloudGet(key: string): Promise<string | null>
  export function cloudSet(key: string, value: string): Promise<void>
  export function cloudRemove(key: string): Promise<void>
  export function onSettingsButton(cb: () => void): () => void   // returns unsubscribe
  export function setClosingGuard(on: boolean): void
  export const bottomButtons: BottomButtons
  // telegramHooks.tsx
  export function useMainButton(options: BottomButtonOptions | null): void
  export function useSecondaryButton(options: BottomButtonOptions | null): void
  export function useDirtyGuard(dirty: boolean): void
  export function BottomButtonsFallback(): JSX.Element | null
  ```

- [ ] **Step 1: Rewrite `telegram.ts`**

Replace `miniapp/src/telegram.ts` with:

```ts
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
  if (insideTelegram) return new Promise((resolve) => webApp!.showConfirm(message, resolve))
  return Promise.resolve(window.confirm(message))
}

export function haptic(type: 'success' | 'error'): void {
  webApp?.HapticFeedback?.notificationOccurred(type)
}

export function selectionHaptic(): void {
  webApp?.HapticFeedback?.selectionChanged()
}

export function impactHaptic(style: 'light' | 'medium' | 'heavy' = 'light'): void {
  webApp?.HapticFeedback?.impactOccurred(style)
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
    button.setParams({ text: o.text, is_active: !o.disabled && !o.loading, has_shine_effect: Boolean(o.shine) })
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

for (const slot of ['main', 'secondary'] as const) telegramButton(slot)?.onClick(() => bottomButtons.click(slot))

/** Whether bottom buttons must be drawn in the page (outside Telegram, or no SecondaryButton). */
export function needsFallback(slot: Slot): boolean {
  return telegramButton(slot) === undefined
}
```

Note: `needsFallback` is part of this module's interface (used by `BottomButtonsFallback`). On clients older than 7.10 the secondary button is drawn in the page.

- [ ] **Step 2: Create the hooks**

Create `miniapp/src/telegramHooks.tsx`:

```tsx
import { useEffect, useSyncExternalStore } from 'react'
import type { BottomButtonOptions, Slot } from './bottomButtons'
import { bottomButtons, needsFallback, setClosingGuard } from './telegram'

function useBottomButton(slot: Slot, options: BottomButtonOptions | null): void {
  // Every render publishes the latest options (text, state, handler) ...
  useEffect(() => bottomButtons.set(slot, options))
  // ... and leaving the page removes the button.
  useEffect(() => () => bottomButtons.set(slot, null), [slot])
}

/** Telegram's main bottom button for this page's primary action; null hides it. */
export function useMainButton(options: BottomButtonOptions | null): void {
  useBottomButton('main', options)
}

/** Telegram's second bottom button (e.g. "Back" in a wizard); null hides it. */
export function useSecondaryButton(options: BottomButtonOptions | null): void {
  useBottomButton('secondary', options)
}

/** Guards unsaved changes: Telegram asks before closing and swipe-to-close is off. */
export function useDirtyGuard(dirty: boolean): void {
  useEffect(() => {
    setClosingGuard(dirty)
    return () => setClosingGuard(false)
  }, [dirty])
}

const subscribe = (listener: () => void) => bottomButtons.subscribe(listener)

/** Draws the bottom buttons in the page when Telegram cannot (a plain browser, old clients). */
export function BottomButtonsFallback() {
  const main = useSyncExternalStore(subscribe, () => bottomButtons.get('main'))
  const secondary = useSyncExternalStore(subscribe, () => bottomButtons.get('secondary'))
  const shown = (['secondary', 'main'] as const)
    .map((slot) => ({ slot, options: slot === 'main' ? main : secondary }))
    .filter(({ slot, options }) => options && needsFallback(slot))
  if (shown.length === 0) return null
  return (
    <div className="bottom-fallback">
      {shown.map(({ slot, options }) => (
        <button
          key={slot}
          className={slot === 'main' ? 'btn' : 'btn secondary'}
          disabled={options!.disabled || options!.loading}
          onClick={() => bottomButtons.click(slot)}
        >
          {options!.loading ? '…' : options!.text}
        </button>
      ))}
    </div>
  )
}
```

- [ ] **Step 3: Use `initTelegram` at startup**

In `miniapp/src/main.tsx` replace:

```ts
import { webApp } from './telegram'

webApp?.ready()
webApp?.expand()
```

with:

```ts
import { initTelegram } from './telegram'

initTelegram()
```

- [ ] **Step 4: Add fallback-bar and safe-area styles**

In `miniapp/src/index.css` replace the `#root` rule with:

```css
#root {
  max-width: 640px;
  margin: 0 auto;
  /* Telegram sets these in full screen; they are 0 otherwise. */
  padding: calc(12px + var(--tg-safe-area-inset-top, 0px) + var(--tg-content-safe-area-inset-top, 0px)) 12px
    calc(32px + var(--tg-safe-area-inset-bottom, 0px));
}
```

and append:

```css
/* Bottom buttons drawn in the page outside Telegram (development) or on old clients. */
.bottom-fallback {
  position: sticky;
  bottom: 0;
  display: flex;
  gap: 8px;
  padding: 8px 0 calc(8px + var(--tg-safe-area-inset-bottom, 0px));
  background: var(--bg2);
}

.bottom-fallback .btn {
  flex: 1;
}
```

- [ ] **Step 5: Type-check and lint**

Run: `cd miniapp && npm run build && npm run lint`
Expected: build succeeds (the hooks are not used yet, which is fine for exported symbols); lint reports 0 errors.

---

### Task 4: Shell with tab bar, stack navigation and credits tab

**Files:**
- Move: `miniapp/src/ui.tsx` → `miniapp/src/ui/index.tsx`
- Create: `miniapp/src/ui/TabBar.tsx`
- Create: `miniapp/src/pages/CreditsPage.tsx`
- Modify: `miniapp/src/App.tsx` (whole file)
- Modify: `miniapp/src/pages/SettingsPage.tsx`, `BotsPage.tsx`, `NewBotPage.tsx`, `MySettingsPage.tsx`, `BotPage.tsx`, `TasksPage.tsx`
- Modify: `miniapp/src/i18n.ts`, `miniapp/src/index.css`

**Interfaces:**
- Consumes: `Route`, `Tab`, `TABS`, `reduce`, `currentTab`, `initialStack`, `isTab` (Task 1); `useMainButton`, `BottomButtonsFallback` (Task 3); `onSettingsButton`, `cloudGet`, `cloudSet`, `selectionHaptic`, `webApp`, `startParam` (Task 3).
- Produces: pages keep the prop `navigate: (r: Route) => void` (now a push); `Route` is imported from `../nav` instead of `../App`. `NewBotPage` gets `navigate` instead of `onDone`. Later tasks rely on `<TabBar active onSelect/>` and `CreditsPage({ me, onChange })`.

- [ ] **Step 1: Move the UI module and add `TabBar`**

```bash
cd miniapp/src && mkdir ui && git mv ui.tsx ui/index.tsx
```

Fix the import in `miniapp/src/ui/index.tsx`: `import { useI18n } from './i18n'` → `import { useI18n } from '../i18n'`, and append:

```ts
export { TabBar } from './TabBar'
```

Create `miniapp/src/ui/TabBar.tsx`:

```tsx
import { useI18n } from '../i18n'
import { TABS, type Tab } from '../nav'

const ICONS: Record<Tab, string> = { bots: '🤖', tasks: '⏰', credits: '💳', settings: '⚙️' }

/** Bottom tab bar, shown on tab roots only (inner pages give the space to Telegram's buttons). */
export function TabBar({ active, onSelect }: { active: Tab; onSelect: (tab: Tab) => void }) {
  const { t } = useI18n()
  return (
    <nav className="tabbar" role="tablist">
      {TABS.map((tab) => (
        <button
          key={tab}
          role="tab"
          aria-selected={tab === active}
          className={tab === active ? 'active' : undefined}
          onClick={() => onSelect(tab)}
        >
          <span className="tabbar-icon" aria-hidden>
            {ICONS[tab]}
          </span>
          {t(`tab.${tab}`)}
        </button>
      ))}
    </nav>
  )
}
```

- [ ] **Step 2: Add tab labels**

In `miniapp/src/i18n.ts`, add to the `en` object (after `settings: 'Settings',`):

```ts
  'tab.bots': 'Bots',
  'tab.tasks': 'Tasks',
  'tab.credits': 'Credits',
  'tab.settings': 'Settings',
  credits: 'Credits',
```

and to the `fa` object (after `settings: 'تنظیمات',`):

```ts
  'tab.bots': 'ربات‌ها',
  'tab.tasks': 'کارها',
  'tab.credits': 'اعتبار',
  'tab.settings': 'تنظیمات',
  credits: 'اعتبار',
```

- [ ] **Step 3: Tab bar styles**

Append to `miniapp/src/index.css`:

```css
/* Room so the fixed tab bar never covers the end of a page. */
.has-tabbar {
  padding-bottom: calc(72px + var(--tg-safe-area-inset-bottom, 0px));
}

.tabbar {
  position: fixed;
  inset-inline: 0;
  bottom: 0;
  display: flex;
  justify-content: center;
  background: var(--bg);
  border-top: 0.5px solid color-mix(in srgb, var(--hint) 30%, transparent);
  padding-bottom: var(--tg-safe-area-inset-bottom, 0px);
  z-index: 10;
}

.tabbar button {
  flex: 1;
  max-width: 160px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 6px 0 8px;
  border: none;
  background: none;
  color: var(--hint);
  font-size: 11px;
}

.tabbar button.active {
  color: var(--link);
}

.tabbar-icon {
  font-size: 22px;
  line-height: 1;
  filter: grayscale(1);
  opacity: 0.7;
}

.tabbar button.active .tabbar-icon {
  filter: none;
  opacity: 1;
}
```

- [ ] **Step 4: Split credits out of `SettingsPage`**

Create `miniapp/src/pages/CreditsPage.tsx` holding, unchanged, the parts of `SettingsPage.tsx` below the language/timezone card: the `packages`/`buying`/`buy` logic, the `quota` helper, the plan/balance/quota card, `<Wallet />`, the "buy credits" section, and the whole `Wallet` component. Its signature and heading:

```tsx
import { useState } from 'react'
import { api, PROVIDER_NAMES, PROVIDERS, type Me, type Provider, type WalletKey } from '../api'
import { useLoad } from '../hooks'
import { formatNumber, useI18n } from '../i18n'
import { haptic, openInvoice } from '../telegram'

/** Credits and payments: balance, today's usage, Stars packages and the user's own provider keys. */
export function CreditsPage({ me, onChange }: { me: Me; onChange: (m: Me) => void }) {
  const { t, lang } = useI18n()
  const [error, setError] = useState<string | null>(null)
  const packages = useLoad(() => api.packages(), [])
  const [buying, setBuying] = useState<string | null>(null)
  // buy() and quota() exactly as they are in SettingsPage today
  return (
    <>
      <h1>{t('credits')}</h1>
      {/* the plan/balance/quota card, <Wallet />, the buy-credits section — moved as is */}
      {error && <p className="error">{error}</p>}
    </>
  )
}
```

(The `{error && …}` line is new: today `buy` errors render inside the language card, which no longer lives here.)

Then reduce `SettingsPage.tsx` to the language/timezone card: delete the moved code and the now-unused imports (`PROVIDER_NAMES`, `PROVIDERS`, `Provider`, `WalletKey`, `useLoad`, `formatNumber`, `haptic`, `openInvoice`); `TIMEZONES`, `zones`, `update` and `error` stay.

- [ ] **Step 5: Rewrite the shell**

Replace `miniapp/src/App.tsx` with:

```tsx
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react'
import { api, type Lang, type Me } from './api'
import { I18n, translator } from './i18n'
import { currentTab, initialStack, isTab, reduce, type Route, type Tab } from './nav'
import { BotPage } from './pages/BotPage'
import { BotsPage } from './pages/BotsPage'
import { CreditsPage } from './pages/CreditsPage'
import { MySettingsPage } from './pages/MySettingsPage'
import { NewBotPage } from './pages/NewBotPage'
import { SettingsPage } from './pages/SettingsPage'
import { TasksPage } from './pages/TasksPage'
import { cloudGet, cloudSet, onSettingsButton, selectionHaptic, startParam, webApp } from './telegram'
import { BottomButtonsFallback } from './telegramHooks'
import { ErrorBox, Loading, TabBar } from './ui'

const LAST_TAB = 'last_tab'

export default function App() {
  const [stack, dispatch] = useReducer(reduce, undefined, () =>
    initialStack(window.location.search, startParam()),
  )
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadMe = useCallback(() => {
    api.me().then(
      (value) => {
        setMe(value)
        setError(null)
      },
      (e: Error) => setError(e.message),
    )
  }, [])
  useEffect(loadMe, [loadMe])

  const lang: Lang = me?.language ?? (webApp?.initDataUnsafe.user?.language_code === 'fa' ? 'fa' : 'en')
  const i18n = useMemo(() => ({ lang, t: translator(lang) }), [lang])

  useEffect(() => {
    document.documentElement.lang = lang
    document.documentElement.dir = lang === 'fa' ? 'rtl' : 'ltr'
  }, [lang])

  const navigate = useCallback((route: Route) => dispatch({ type: 'push', route }), [])
  const selectTab = useCallback((tab: Tab) => {
    selectionHaptic()
    dispatch({ type: 'tab', tab })
  }, [])

  // Reopen on the tab used last time, unless Telegram opened a specific page.
  useEffect(() => {
    if (stack.length > 1) return
    cloudGet(LAST_TAB).then((tab) => {
      if (tab && isTab(tab) && tab !== 'bots') dispatch({ type: 'tab', tab })
    })
    // only on first load
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const tab = currentTab(stack)
  useEffect(() => {
    cloudSet(LAST_TAB, tab)
  }, [tab])

  // Telegram's back button goes one page back; hidden on tab roots.
  useEffect(() => {
    const back = webApp?.BackButton
    if (!back) return
    const goBack = () => dispatch({ type: 'back' })
    if (stack.length > 1) back.show()
    else back.hide()
    back.onClick(goBack)
    return () => back.offClick(goBack)
  }, [stack.length])

  // "Settings" in the Mini App's own menu opens the settings tab.
  useEffect(() => onSettingsButton(() => dispatch({ type: 'tab', tab: 'settings' })), [])

  const route = stack[stack.length - 1]
  const onTabRoot = stack.length === 1

  let content
  if (error) content = <ErrorBox message={error} onRetry={loadMe} />
  else if (!me) content = <Loading />
  else if (route.page === 'bot') content = <BotPage id={route.id} me={me} navigate={navigate} />
  else if (route.page === 'mine') content = <MySettingsPage id={route.id} navigate={navigate} />
  else if (route.page === 'new') content = <NewBotPage navigate={navigate} />
  else if (route.page === 'settings') content = <SettingsPage me={me} onChange={setMe} />
  else if (route.page === 'credits') content = <CreditsPage me={me} onChange={setMe} />
  else if (route.page === 'tasks') content = <TasksPage me={me} />
  else content = <BotsPage me={me} navigate={navigate} />

  return (
    <I18n.Provider value={i18n}>
      <div className={onTabRoot ? 'has-tabbar' : undefined}>{content}</div>
      <BottomButtonsFallback />
      {onTabRoot && <TabBar active={tab} onSelect={selectTab} />}
    </I18n.Provider>
  )
}
```

`oxlint` does not know the `react-hooks/exhaustive-deps` rule; if it reports the disable comment as unknown, delete the comment line (keep "only on first load").

- [ ] **Step 6: Point the pages at the new navigation**

In each of `BotsPage.tsx`, `BotPage.tsx`, `MySettingsPage.tsx` change `import type { Route } from '../App'` to `import type { Route } from '../nav'`.

`MySettingsPage.tsx` line ~80: the wallet now lives in the credits tab:

```tsx
<button className="btn secondary" onClick={() => navigate({ page: 'credits' })}>
```

`BotsPage.tsx`: the tab bar replaces the header buttons, and "new bot" moves to the MainButton. Replace the header block

```tsx
      <div className="row between">
        <h1>{t('myBots')}</h1>
        <div className="row">
          ...
        </div>
      </div>
```

with

```tsx
      <h1>{t('myBots')}</h1>
```

delete

```tsx
      <button className="btn block" onClick={() => navigate({ page: 'new' })}>
        + {t('newBot')}
      </button>
```

and add at the top of the `BotsPage` body (after `const quota = …`):

```tsx
  useMainButton({ text: `＋ ${t('newBot')}`, onClick: () => navigate({ page: 'new' }) })
```

with `import { useMainButton } from '../telegramHooks'`.

`NewBotPage.tsx`: change the signature to `export function NewBotPage({ navigate }: { navigate: (r: Route) => void })` (import `Route` from `../nav`); in `create`, replace `onDone()` with `navigate({ page: 'bots' })`; remove the `<button className="btn block" …>{t('createInTelegram')}</button>` element and add after the `create` function:

```tsx
  useMainButton({
    text: t('createInTelegram'),
    onClick: create,
    disabled: !form.name.trim() || !form.system_prompt.trim(),
    loading: busy,
  })
  useDirtyGuard(Boolean(form.name || form.description || form.system_prompt))
```

with `import { useDirtyGuard, useMainButton } from '../telegramHooks'`.

`TasksPage.tsx`: the delete button asks first, with Telegram's red-button popup. Change `onDelete={() => run(() => api.deleteTask(task.id))}` to

```tsx
          onDelete={async () => {
            if (await confirm(t('deleteTaskConfirm'), { action: t('delete'), destructive: true }))
              run(() => api.deleteTask(task.id))
          }}
```

with `import { confirm, haptic } from '../telegram'`, and add i18n keys — `en`: `deleteTaskConfirm: 'Delete this task? It will not run again.'`, `fa`: `deleteTaskConfirm: 'این کار حذف بشه؟ دیگه اجرا نمی‌شه.'`.

`BotsPage.tsx` `UsedBotItem.remove`: give its confirm a named destructive button:

```tsx
    if (!(await confirm(t('removeUsedConfirm', { username: bot.username }), { action: t('removeFromList'), destructive: true }))) return
```

- [ ] **Step 7: Build, lint and test**

Run: `cd miniapp && npm run build && npm run lint && npm test`
Expected: build succeeds, lint 0 errors, 13 tests pass.

- [ ] **Step 8: Check in the browser**

1. Refresh dev initData (expires after 24h): `cd backend && uv run python -m obot.api.dev_init_data 272476753 > ../miniapp/.env.local`
2. Start the `api` and `miniapp` preview servers from `.claude/launch.json` (use `api-alt`/`miniapp-alt` if 8000/5173 are taken).
3. Open `http://localhost:5173` and check:
   - tab bar with four tabs; each tab opens its page; the credits tab shows balance, packages and wallet; settings shows only language/timezone;
   - the in-page fallback bar shows "＋ New bot" on the bots tab; clicking it opens the new-bot page, where the tab bar is hidden and the fallback bar shows "Create in Telegram" (disabled until name and instructions are filled);
   - `?bot=<id of one of your bots>` opens that bot's page directly; `?mine=<id>` opens your settings for it;
   - switching to fa flips the tab bar order (RTL) and labels.

- [ ] **Step 9: Ask the user to test in Telegram**

The user opens the panel from the factory bot's menu button and checks: tab bar; MainButton "＋ ربات جدید" at the bottom; BackButton goes one page back (bot page → my settings → back → bot page → back → list); "Settings" in the Mini App's ⋮ menu opens the settings tab; reopening the panel returns to the last tab; the new-bot form asks before closing when something is typed; deleting a task shows a red "Delete" popup; header and bottom bar colours match the page in light and dark themes.

Report honestly which of these were checked and which were not.

- [ ] **Step 10: Commit (after the user confirms)**

```bash
git add miniapp docs/superpowers/plans/2026-10-07-ux1-miniapp-foundation.md
git commit -m "Give the panel a Telegram-native shell with tabs and bottom buttons

Tab bar (bots, tasks, credits, settings) over a stack router driven by
Telegram's BackButton; primary actions on MainButton with an in-page
fallback outside Telegram; red-button popups, SettingsButton, last tab
remembered in CloudStorage, closing guard for unsaved forms, theme-matched
header and bottom bar. Credits and wallet move to their own tab.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
