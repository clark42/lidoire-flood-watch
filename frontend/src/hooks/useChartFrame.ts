// Small hooks shared by the hand-drawn SVG charts (profile, strips, phase
// plane); their width comes from useElementWidth.ts
import { useEffect, useRef, useState, type RefObject } from 'react'

/** true when the user asked the system for reduced motion (followed live). */
export function usePrefersReducedMotion(): boolean {
  const query = '(prefers-reduced-motion: reduce)'
  const [reduce, setReduce] = useState(
    () => typeof window !== 'undefined' && !!window.matchMedia?.(query).matches,
  )
  useEffect(() => {
    const mq = window.matchMedia?.(query)
    if (!mq) return
    const onChange = () => setReduce(mq.matches)
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  return reduce
}

/**
 * Becomes true once the element comes near the viewport (and stays true):
 * the panels below the fold only fetch when the reader gets there.
 */
export function useInView<T extends HTMLElement>(rootMargin = '600px'): [RefObject<T>, boolean] {
  const ref = useRef<T>(null)
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    if (seen) return
    const el = ref.current
    if (!el || typeof IntersectionObserver === 'undefined') {
      setSeen(true)
      return
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) setSeen(true)
      },
      { rootMargin },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [seen, rootMargin])
  return [ref, seen]
}
