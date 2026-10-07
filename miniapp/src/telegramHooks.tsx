import { useEffect } from 'react'
import type { BottomButtonOptions, Slot } from './bottomButtons'
import { bottomButtons, setClosingGuard } from './telegram'

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
