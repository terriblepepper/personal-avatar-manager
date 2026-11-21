export function bindDisclosures(signal: AbortSignal) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)')
  const active = new WeakMap<HTMLDetailsElement, Animation[]>()
  const nativeTransition = CSS.supports('selector(details::details-content)') && CSS.supports('transition-behavior','allow-discrete')
  function summaryOf(details: HTMLDetailsElement) { return details.querySelector<HTMLElement>(':scope > summary') }
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented || !(event.target instanceof Element)) return
    const details = event.target.closest<HTMLDetailsElement>('details[open]')
    if (!details) return
    event.preventDefault(); event.stopImmediatePropagation()
    const summary = summaryOf(details)
    summary?.click(); summary?.focus({preventScroll:true})
  }, {signal})
  if (nativeTransition) return
  document.addEventListener('click',event => {
    const summary = event.target instanceof Element ? event.target.closest('summary') : null
    const details = summary?.parentElement
    if (!(details instanceof HTMLDetailsElement) || !summary || reduced.matches || event.defaultPrevented) return
    event.preventDefault()
    const pending = active.get(details)
    pending?.forEach(effect => effect.cancel())
    const closing = details.open && !pending
    details.open = true
    const effects = Array.from(details.children).filter(el=>el!==summary).map(el=>el.animate(
      [{opacity:closing ? 1 : 0},{opacity:closing ? 0 : 1}], {duration:180,easing:'ease'}))
    if (closing) active.set(details,effects); else active.delete(details)
    void Promise.allSettled(effects.map(effect => effect.finished)).then(() => {
      if (closing && active.get(details) === effects) { active.delete(details); details.open=false }
    })
  }, {signal})
}
