import { useEffect, useRef, useState } from 'react'

/**
 * Width (px) of an element, kept up to date with a ResizeObserver. SVG
 * charts draw at their real width so that 11-12 px labels stay 11-12 px on a
 * phone (a scaled viewBox would shrink them). `initial` is used for the
 * server render (tests) and the first paint.
 */
export function useElementWidth<T extends HTMLElement>(initial = 343) {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(initial)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const w = Math.floor(el.getBoundingClientRect().width)
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
