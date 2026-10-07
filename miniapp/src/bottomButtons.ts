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
