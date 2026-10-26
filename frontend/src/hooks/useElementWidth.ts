import { useEffect, useRef, useState } from 'react'

/**
 * Width (px) of an element, followed with a ResizeObserver. `initial` is
 * used before the first measure (and on the server), e.g. a phone width.
 */
export function useElementWidth<T extends HTMLElement>(initial = 343) {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(initial)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const w = Math.floor(el.clientWidth)
      if (w > 0) setWidth((prev) => (prev === w ? prev : w))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return [ref, width] as const
}
