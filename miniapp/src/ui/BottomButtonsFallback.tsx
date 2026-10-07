import { useSyncExternalStore } from 'react'
import { bottomButtons, needsFallback } from '../telegram'

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
