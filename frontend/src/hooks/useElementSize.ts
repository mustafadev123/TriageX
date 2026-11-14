import { useLayoutEffect, useRef, useState } from 'react'

/** Measures an element so charts and the map size themselves to the projector layout. */
export function useElementSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })

  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const update = () => {
      const rect = el.getBoundingClientRect()
      setSize((s) =>
        Math.round(s.width) === Math.round(rect.width) &&
        Math.round(s.height) === Math.round(rect.height)
          ? s
          : { width: rect.width, height: rect.height },
      )
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return [ref, size] as const
}
