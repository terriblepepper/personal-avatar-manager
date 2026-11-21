// PDF text positions use page coordinates. Percentage boxes follow CSS zoom and rotation.
type TextItem = { str: string; width: number; transform: number[] }
function isTextItem(item: unknown): item is TextItem {
  return !!item && typeof item === 'object' && 'str' in item && typeof item.str === 'string'
    && 'width' in item && typeof item.width === 'number' && 'transform' in item && Array.isArray(item.transform)
}
export const normalize = (value: string) => value.normalize('NFKC').toLocaleLowerCase().replace(/\s+/g, '')
export function pdfMatches(items: unknown[], query: string, viewport: {
  width: number; height: number; convertToViewportPoint(x: number, y: number): number[]
}) {
  const needle = normalize(query)
  if (!needle) return []
  const runs = items.filter(isTextItem).map(item => ({item, text:normalize(item.str)}))
  const text = runs.map(run => run.text).join('')
  const starts: number[] = []
  for (let i = text.indexOf(needle); i !== -1 && starts.length < 200; i = text.indexOf(needle, i + needle.length)) starts.push(i)
  let offset = 0, hit = 0
  const boxes = []
  for (const { item, text } of runs) {
    const end = offset + text.length
    while (hit < starts.length && starts[hit]! + needle.length <= offset) hit++
    if (text && hit < starts.length && starts[hit]! < end) {
      const [a,b,c,d,x,y] = item.transform as [number,number,number,number,number,number]
      const length = Math.hypot(a,b) || 1, dx = a / length * item.width, dy = b / length * item.width
      const corners = [[x,y],[x+dx,y+dy],[x+c,y+d],[x+dx+c,y+dy+d]].map(([px,py]) => viewport.convertToViewportPoint(px!,py!))
      const xs = corners.map(p => p[0]!), ys = corners.map(p => p[1]!)
      boxes.push({left:Math.min(...xs)/viewport.width*100+'%', top:Math.min(...ys)/viewport.height*100+'%',
        width:(Math.max(...xs)-Math.min(...xs))/viewport.width*100+'%', height:(Math.max(...ys)-Math.min(...ys))/viewport.height*100+'%'})
    }
    offset = end
  }
  return boxes
}

export function markText(root: HTMLElement, query: string) {
  if (typeof Highlight === 'undefined' || !CSS.highlights || !query) return
  const highlight = CSS.highlights.get('preview-match') || new Highlight()
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), ranges: Range[] = []
  const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu')
  for (let node = walker.nextNode(); node && ranges.length < 200; node = walker.nextNode()) {
    for (const match of (node.textContent || '').matchAll(pattern)) {
      if (ranges.length >= 200) break
      const range = document.createRange()
      range.setStart(node,match.index!); range.setEnd(node,match.index! + match[0].length)
      highlight.add(range); ranges.push(range)
    }
  }
  CSS.highlights.set('preview-match',highlight)
  const first = ranges[0], body = root.closest<HTMLElement>('.preview-document')
  if (first && body) body.scrollTop += first.getBoundingClientRect().top - body.getBoundingClientRect().top - body.clientHeight / 2
  return () => {
    ranges.forEach(range => highlight.delete(range))
    if (!highlight.size) CSS.highlights.delete('preview-match')
  }
}
