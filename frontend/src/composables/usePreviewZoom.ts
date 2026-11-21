import { computed, nextTick, ref, watch, type Ref } from 'vue'

export function usePreviewZoom(body: Ref<HTMLElement | undefined>, sheet: Ref<HTMLElement | undefined>, kind: Ref<string>) {
  const available = ref(0), naturalWidth = ref(0), manualScale = ref<number | null>(null)
  const availableHeight = ref(0), naturalHeight = ref(0)
  const fit = computed(() => manualScale.value === null)
  const scale = computed(() => {
    if (manualScale.value !== null) return manualScale.value
    if (!naturalWidth.value || !available.value) return 1
    const ratio = available.value / naturalWidth.value
    if (kind.value === 'image' && naturalHeight.value && availableHeight.value) {
      return Math.min(ratio, availableHeight.value / naturalHeight.value)
    }
    return kind.value === 'image' ? Math.min(1, ratio) : ratio
  })
  const percent = computed(() => Number((scale.value * 100).toFixed(2)))

  watch(body, (node, _, cleanup) => {
    if (!node) return
    const measure = () => {
      const style = getComputedStyle(node)
      availableHeight.value = Math.max(0, node.clientHeight - parseFloat(style.paddingTop || '0') - parseFloat(style.paddingBottom || '0'))
      available.value = Math.max(0, node.clientWidth - parseFloat(style.paddingLeft || '0') - parseFloat(style.paddingRight || '0'))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    cleanup(() => observer.disconnect())
  }, { flush: 'post' })

  function geometry() {
    const viewport = body.value, content = sheet.value
    if (!viewport || !content) return
    const frame = viewport.getBoundingClientRect(), box = content.getBoundingClientRect()
    if (!box.width || !box.height) return
    return { box, x: frame.left + viewport.clientLeft + viewport.clientWidth / 2,
      y: frame.top + viewport.clientTop + viewport.clientHeight / 2 }
  }
  let revision = 0
  function setScale(value: number | null) {
    const previous = geometry(), ticket = ++revision
    manualScale.value = value === null ? null : Math.max(.0001, Math.min(100, value))
    if (!previous) return
    // Preserve the document point under the viewport centre after Vue applies the new size.
    const fx = (previous.x - previous.box.left) / previous.box.width
    const fy = (previous.y - previous.box.top) / previous.box.height
    void nextTick(() => {
      const current = geometry(), viewport = body.value
      if (ticket !== revision || !current || !viewport) return
      viewport.scrollLeft += current.box.left + fx * current.box.width - current.x
      viewport.scrollTop += current.box.top + fy * current.box.height - current.y
    })
  }
  function resetZoom() { revision++; naturalWidth.value = 0; naturalHeight.value = 0; manualScale.value = null }
  return { available, naturalWidth, naturalHeight, fit, scale, percent, setScale, resetZoom }
}
