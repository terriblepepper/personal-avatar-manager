import type { Ref } from 'vue'

/** Keep the first visible message in place when older messages are prepended. */
export function useHistoryScroll(log: Ref<HTMLElement | undefined>, loadOlder: () => void) {
  let lastTop = 0
  let touchY: number | undefined

  function atStart() {
    if (log.value && log.value.scrollTop <= 80) loadOlder()
  }
  function captureAnchor() {
    const element = log.value
    if (!element) return () => {}
    const viewportTop = () => element.getBoundingClientRect().top + element.clientTop
    const top = viewportTop()
    const anchor = Array.from(element.querySelectorAll<HTMLElement>('[data-history-message]'))
      .find(row => row.getBoundingClientRect().bottom > top)
    if (!anchor) return () => {}
    const previousOffset = anchor.getBoundingClientRect().top - top
    return () => {
      if (element === log.value && element.contains(anchor))
        element.scrollTop += anchor.getBoundingClientRect().top - viewportTop() - previousOffset
    }
  }

  const historyEvents = {
    scrollPassive() {
      const top = log.value?.scrollTop ?? 0
      if (top < lastTop) atStart()
      lastTop = top
    },
    wheelPassive(event: WheelEvent) { if (!event.ctrlKey && event.deltaY < 0) atStart() },
    touchstartPassive(event: TouchEvent) {
      touchY = event.touches.length === 1 ? event.touches[0]?.clientY : undefined
    },
    touchmovePassive(event: TouchEvent) {
      const y = event.touches.length === 1 ? event.touches[0]?.clientY : undefined
      if (y !== undefined && touchY !== undefined && y > touchY) atStart()
      touchY = y
    },
    keydown(event: KeyboardEvent) {
      const target = event.target
      if (target instanceof HTMLElement && (target === log.value || target.matches('[data-history-message]'))
          && ['Home','PageUp','ArrowUp'].includes(event.key)) atStart()
    },
  }
  return { historyEvents, captureAnchor }
}
