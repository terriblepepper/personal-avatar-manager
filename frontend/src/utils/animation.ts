export const MAX_ANIMATION_BYTES = 100 * 1024 * 1024
export function animationUploadError(files: readonly File[]): string {
  const file = files.find(item => item.size > MAX_ANIMATION_BYTES && /\.(?:avanim|html?)$/i.test(item.name))
  return file ? `文件「${file.name}」超过 100 MiB` : ''
}
const edgeKinds = new Set(['device', 'browser', 'router', 'server', 'cloud', 'database', 'network', 'agent'])
const nodeKinds = new Set([...edgeKinds, 'person', 'document', 'queue', 'decision', 'shield'])
const templates = new Set(['classic', 'blueprint', 'timeline', 'orbit'])
const markers = new Set(['dot', 'arrow', 'envelope', 'pulse'])
const unsafeText = /[<>\u0000-\u001f\u007f\ud800-\udfff]|(?::\/\/|\/\/[\w.-]+|(?:data|javascript|vbscript)\s*:|\bon[a-z]+\s*=|\b(?:url|expression)\s*\(|\bstyle\s*=|\{\s*(?:--)?[a-z][\w-]*\s*:|@(?:import|font-face|media|keyframes|supports|layer|namespace|page|charset)\b)/iu

function fail(field: string): never { throw Error('动画包字段无效：' + field) }
function record(value: unknown, keys: string, path: string) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) fail(path)
  const allowed = keys.split(' ')
  for (const key of Object.keys(value)) if (!allowed.includes(key)) fail(path + '.' + key)
  return value as Record<string, unknown>
}
function text(value: unknown, limit: number, path: string) {
  if (typeof value !== 'string' || value.length > limit * 2 || [...value].length > limit || !value.trim() || unsafeText.test(value)) fail(path)
  return value.trim()
}
function id(value: unknown, path: string) {
  if (typeof value !== 'string' || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) fail(path)
  return value
}
function number(value: unknown, min: number, max: number, path: string, integer = true) {
  if (typeof value !== 'number' || !Number.isFinite(value) || (integer && !Number.isInteger(value)) || value < min || value > max) fail(path)
  return value
}
function list(value: unknown, limit: number, path: string, min = 0): unknown[] {
  if (!Array.isArray(value) || value.length < min || value.length > limit) fail(path)
  return value
}
function choice(value: unknown, allowed: Set<string>, path: string) {
  if (typeof value !== 'string' || !allowed.has(value)) fail(path)
  return value
}
const optional = (value: unknown, fallback: unknown) => value === undefined ? fallback : value

// Bound object allocation before JSON.parse and reject duplicate fields that JSON.parse would discard.
function checkStructure(source: string) {
  const scopes: (Set<string> | null)[] = []
  const tokens = /[{}[\],"]/g, colon = /[ \t\r\n]*:/y
  let containers = 0, arraySeparators = 0
  for (let token; (token = tokens.exec(source));) {
    const mark = token[0]
    if (mark === '{' || mark === '[') {
      if (scopes.length >= 5) fail('嵌套深度')
      // 5000 drawing objects, three document objects, four root lists and three lists per step.
      if (++containers > 5000 + 3 + 4 + 200 * 3) fail('绘图结构超限')
      scopes.push(mark === '{' ? new Set() : null)
    } else if (mark === '}' || mark === ']') {
      if (!scopes.length || (scopes.pop() === null) !== (mark === ']')) fail('JSON 结构')
    } else if (mark === ',') {
      // v1 permits 5000 visual elements plus 1500 focus IDs and 32 notes per step, and 32 tags.
      if (scopes.at(-1) === null && ++arraySeparators > 5000 + 200 * (1500 + 32) + 32) fail('数组元素超限')
    } else {
      const start = tokens.lastIndex
      let end = start
      for (; end < source.length && source[end] !== '"'; end++) {
        if (source[end] === '\\') end++
        // Each of the 2000 permitted code points may use two escaped UTF-16 code units.
        if (end - start >= 2000 * 12) fail('文字过长')
      }
      if (end >= source.length) fail('JSON 字符串')
      tokens.lastIndex = colon.lastIndex = end + 1
      if (!colon.test(source)) continue
      const keys = scopes.at(-1)
      if (!keys || keys.size >= 13 || end - start > 13 * 6) fail('对象字段')
      const key: string = JSON.parse(source.slice(start - 1, end + 1))
      if (keys.has(key)) fail('重复字段 ' + key)
      keys.add(key)
    }
  }
}

export function prepareAnimation(source: string) {
  let bytes = source.length
  if (bytes > MAX_ANIMATION_BYTES) fail('源码超过 100 MiB')
  // UTF-16 length bounds UTF-8 at three bytes per code unit, without encoding a second copy.
  if (bytes > MAX_ANIMATION_BYTES / 3) {
    for (let i = source.search(/[^\x00-\x7f]/); i >= 0 && i < source.length; i++) {
      const cp = source.codePointAt(i)!
      if (cp > 0x7ff) bytes += 2
      else if (cp > 0x7f) bytes++
      if (cp > 0xffff) i++
      if (bytes > MAX_ANIMATION_BYTES) fail('源码超过 100 MiB')
    }
  }
  checkStructure(source)
  let data: unknown
  try { data = JSON.parse(source) } catch { throw Error('动画包 JSON 不完整或语法错误') }
  const root = record(data, 'format version title summary tags canvas durationMs loop accessibility nodes edges steps template', 'scene')
  if (root.format !== 'acps.animation' || root.version !== 1) fail('format/version')
  const template = choice(optional(root.template, 'classic'), templates, 'template')
  const frame = record(root.canvas, 'width height background', 'canvas')
  const color = optional(frame.background, 'paper')
  if (color !== 'paper' && color !== 'dark' && color !== 'light') fail('canvas.background')
  const canvas = {width:number(frame.width, 1, 4096, 'width'), height:number(frame.height, 1, 4096, 'height'), background:color}
  const durationMs = number(root.durationMs, 1, 60000, 'durationMs')
  const loop = optional(root.loop, false)
  if (typeof loop !== 'boolean') fail('loop')
  const access = record(root.accessibility, 'alt reducedMotion', 'accessibility')
  if (optional(access.reducedMotion, 'final-frame') !== 'final-frame') fail('accessibility.reducedMotion')
  const ids = new Set<string>()
  function unique(value: unknown) {
    const key = id(value, 'id')
    if (ids.has(key)) fail('重复 ID ' + key)
    ids.add(key)
    return key
  }
  const nodes = list(root.nodes, 500, 'nodes', 1).map(value => {
    const n = record(value, 'id label kind x y', 'node')
    return {id:unique(n.id), label:text(n.label, 120, 'node.label'), kind:choice(n.kind, nodeKinds, 'node.kind'),
      x:number(n.x, 0, canvas.width, 'node.x', false), y:number(n.y, 0, canvas.height, 'node.y', false)}
  })
  const nodeIds = new Set(nodes.map(n => n.id))
  function node(value: unknown) {
    const key = id(value, '节点引用')
    if (!nodeIds.has(key)) fail('未知节点 ' + key)
    return key
  }
  const edges = list(root.edges, 1000, 'edges').map(value => {
    const e = record(value, 'id from to kind', 'edge')
    return {id:unique(e.id), from:node(e.from), to:node(e.to), kind:choice(e.kind, edgeKinds, 'edge.kind')}
  })
  const byEdge = new Map(edges.map(e => [e.id, e]))
  let total = nodes.length + edges.length, latest = 0
  const steps = list(root.steps, 200, 'steps', 1).map(value => {
    const s = record(value, 'id title detail at duration focus packets notes', 'step')
    const at = number(s.at, latest, durationMs, 'step.at')
    latest = at
    const duration = number(s.duration, 1, durationMs - at, 'step.duration')
    const focus = list(optional(s.focus, []), 1500, 'focus').map(item => {
      const key = id(item, 'focus')
      if (!nodeIds.has(key) && !byEdge.has(key)) fail('未知焦点 ' + key)
      return key
    })
    const packets = list(optional(s.packets, []), 5000, 'packets').map(item => {
      const p = record(item, 'from to edge label marker', 'packet')
      const key = id(p.edge, 'packet.edge'), edge = byEdge.get(key), from = node(p.from), to = node(p.to)
      if (!edge || !(edge.from === from && edge.to === to || edge.from === to && edge.to === from)) fail('packet.edge')
      return {from, to, edge:key, label:text(p.label, 120, 'packet.label'), marker:choice(optional(p.marker, 'dot'), markers, 'packet.marker')}
    })
    total += 1 + packets.length
    if (total > 5000) fail('绘图元素超过 5000 个')
    return {id:unique(s.id), title:text(s.title, 160, 'step.title'), detail:text(s.detail, 2000, 'step.detail'), at, duration, focus, packets,
      notes:list(optional(s.notes, []), 32, 'notes').map(n => text(n, 500, 'note'))}
  })
  const scene = {format:'acps.animation', version:1, template, title:text(root.title, 160, 'title'), summary:text(root.summary, 2000, 'summary'),
    tags:list(optional(root.tags, []), 32, 'tags').map(t => text(t, 120, 'tag')), canvas, durationMs, loop,
    accessibility:{alt:text(access.alt, 2000, 'alt'), reducedMotion:'final-frame'}, nodes, edges, steps}
  // Drop JSON formatting on large transfers; preserve original field values for the server's digest.
  return {scene, source:source.length > 1024 * 1024 ? JSON.stringify(data) : source}
}
export function parseAnimation(source: string) { return prepareAnimation(source).scene }
export type AnimationScene = ReturnType<typeof parseAnimation>
