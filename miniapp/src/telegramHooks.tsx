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
