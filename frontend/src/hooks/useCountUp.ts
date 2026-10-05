import { useEffect, useRef, useState } from 'react'

const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3)

/**
 * 数字滚动：目标值变化时从上一个值平滑滚过去（1Panel 总览同款手感）。
 * target 为 null（还没取到数）显示 '—'；系统声明减弱动效时直接跳到终值。
 */
export function useCountUp(target: number | null, duration = 600): string {
  const [display, setDisplay] = useState('—')
  const fromRef = useRef(0)
  const rafRef = useRef(0)

  useEffect(() => {
    if (target == null || !Number.isFinite(target)) {
      setDisplay('—')
      return
    }
    const to = target
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduce) {
      fromRef.current = to
      setDisplay(String(to))
      return
    }
    const from = fromRef.current
    if (from === to) {
      setDisplay(String(to))
      return
    }
    const start = performance.now()
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      setDisplay(String(Math.round(from + (to - from) * easeOutCubic(t))))
      if (t < 1) rafRef.current = requestAnimationFrame(tick)
      else fromRef.current = to
    }
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [target, duration])

  return display
}
