import { useCallback, useEffect, useState } from 'react'

/** Loads data with `load`; `reload` refetches and `set` replaces it after a mutation. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps)
  const reload = useCallback(() => {
    run().then(
      (value) => {
        setData(value)
        setError(null)
      },
      (e: Error) => setError(e.message),
    )
  }, [run])
  useEffect(reload, [reload])
  return { data, error, reload, set: setData }
}
