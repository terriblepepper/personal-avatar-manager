(() => {
  'use strict'
  const svg = document.querySelector('svg'), channel = location.hash.slice(1)
  const ns = 'http://www.w3.org/2000/svg'
  if (!channel || parent === window) return
  const send = (type, detail = {}) => parent.postMessage({ avanim: true, channel, type, ...detail }, '*')
  const glyph = {
    device:'M-10-8h20V6h-20z M0 6v4 M-6 10H6',
    browser:'M-11-9h22v18h-22z M-11-3h22 M-7-6h1 M-3-6h1',
    server:'M-10-10h20v8h-20z M-10 2h20v8h-20z M-6-6h2 M-6 6h2',
    router:'M-11 1h22v8h-22z M-8 1v-8 M8 1v-8 M-6 5h2 M1 5h4',
    cloud:'M-8 8c-8 0-8-10-2-11 1-10 14-11 17-2 9-1 11 13 2 13z',
    database:'M-9-7c0-5 18-5 18 0s-18 5-18 0v14c0 5 18 5 18 0V-7 M-9 0c0 5 18 5 18 0',
    network:'M0-7l-8 14h16z M-2-11h4v4h-4z M-11 5h5v5h-5z M6 5h5v5H6z',
    agent:'M-10-7h20v15h-20z M0-7v-4 M-5-2v2 M5-2v2 M-4 4h8',
    person:'M-5-5a5 5 0 1 0 10 0a5 5 0 1 0-10 0 M-10 9c0-9 20-9 20 0',
    document:'M-8-10h11l6 6v14H-8z M3-10v6h6 M-4 1h9 M-4 5h6',
    queue:'M-11-8h14v3 M-8-4H6v3 M-5 0H9v9H-5z M-2 4h6',
    decision:'M0-11l9 8-9 8-9-8z M0 5v3 M0 8h-7v3 M0 8h7v3',
    shield:'M0-11l9 4v7c0 5-4 8-9 11-5-3-9-6-9-11v-7z M-4 0l3 3 5-6',
  }
  let scene, reduced = false, playing = false, elapsed = 0, speed = 1, step = -1
  let frame = 0, previous = 0, notified = 0, markers = [], packetsLayer, scale = 1
  const positions = new Map(), items = new Map(), routes = new Map()
  let focus = new Set(), segmentStart = -1, segmentEnd = -1
  function element(tag, attrs = {}, label) {
    const n = document.createElementNS(ns, tag)
    for (const [key, value] of Object.entries(attrs)) n.setAttribute(key, String(value))
    if (label !== undefined) n.textContent = label
    return n
  }
  function nodeShape(template) {
    const cls = {class:'node-box'}
    if (template === 'blueprint') return [
      element('rect', {...cls, x:-20, y:-16, width:40, height:32, rx:2}),
      element('path', {class:'node-detail', d:'M-15-19v-4m10 4v-4m10 4v-4m10 4v-4 M-15 19v4m10-4v4m10-4v4m10-4v4'}),
    ]
    if (template === 'timeline') return [
      element('path', {...cls, d:'M-23-15H12L24 0 12 15h-35l6-15z'}),
      element('path', {class:'node-detail', d:'M0 16v7'}),
    ]
    if (template === 'orbit') return [
      element('circle', {...cls, r:19}),
      element('ellipse', {class:'node-detail', rx:27, ry:11, transform:'rotate(-28)'}),
    ]
    return [element('rect', {...cls, x:-21,y:-19,width:42,height:38,rx:9})]
  }
  function packetShape(marker) {
    if (marker === 'arrow') return element('path', {class:'packet-dot', d:'M-8-5 9 0-8 5-5 0z'})
    if (marker === 'envelope') return element('path', {class:'packet-dot', d:'M-9-6H9V6H-9z M-9-5 0 1 9-5'})
    if (marker === 'pulse') {
      const pulse = element('g')
      pulse.append(element('circle', {class:'pulse-ring', r:9}), element('circle', {class:'packet-dot', r:4}))
      return pulse
    }
    return element('circle', {class:'packet-dot', r:6})
  }
  function makeRoute(a, b, template) {
    const dx = b.x-a.x, dy = b.y-a.y, length = Math.hypot(dx,dy)
    const route = {a,b,dx,dy,template,ox:0,oy:0,radius:0}
    const attrs = {class:'edge', 'vector-effect':'non-scaling-stroke'}
    let line
    if (template !== 'classic' && !length) {
      route.radius = 26 * scale
      line = element('circle', {...attrs,cx:a.x,cy:a.y-route.radius,r:route.radius})
    } else if (template === 'blueprint') {
      route.travel = Math.abs(dx) + Math.abs(dy)
      line = element('path', {...attrs,d:`M ${a.x} ${a.y} H ${b.x} V ${b.y}`})
    } else if (template === 'orbit') {
      const bend = Math.min(length * .24, 75 * scale) / length
      route.ox = -dy * bend
      route.oy = dx * bend
      line = element('path', {...attrs,d:`M ${a.x} ${a.y} Q ${(a.x+b.x)/2+route.ox} ${(a.y+b.y)/2+route.oy} ${b.x} ${b.y}`})
    } else line = element('line', {...attrs,x1:a.x,y1:a.y,x2:b.x,y2:b.y})
    return {route, line}
  }
  function movePacket(marker, fraction) {
    const {route, group, symbol, reverse, directional} = marker
    const t = reverse ? 1 - fraction : fraction
    const {a,b,dx,dy,ox,oy,radius} = route
    let x = a.x + dx*t, y = a.y + dy*t, vx = dx, vy = dy
    if (radius) {
      const angle = t * Math.PI * 2
      x = a.x + radius * Math.sin(angle)
      y = a.y + radius * (Math.cos(angle) - 1)
      vx = Math.cos(angle)
      vy = -Math.sin(angle)
    } else if (route.template === 'blueprint') {
      const distance = t * route.travel
      if (distance < Math.abs(dx) || !dy) { x = a.x + distance*Math.sign(dx); y = a.y; vy = 0 }
      else { x = b.x; y = a.y + (distance-Math.abs(dx))*Math.sign(dy); vx = 0 }
    } else if (route.template === 'orbit') {
      const curve = 2*t*(1-t)
      x += curve*ox; y += curve*oy
      vx += 2*(1-2*t)*ox; vy += 2*(1-2*t)*oy
    }
    group.setAttribute('transform', 'translate(' + x + ' ' + y + ') scale(' + scale + ')')
    if (directional) symbol.setAttribute('transform', 'rotate(' + (Math.atan2(vy,vx)*180/Math.PI + (reverse ? 180 : 0)) + ')')
  }
  function state(type = 'state') { send(type, { elapsed, step, playing, speed }) }
  function pause() {
    playing = false
    cancelAnimationFrame(frame)
    svg.classList.remove('playing')
    for (const a of svg.getAnimations?.({subtree:true}) || []) a.cancel()
  }
  function activeStep() {
    let i = scene.steps.length - 1
    while (i >= 0 && scene.steps[i].at > elapsed) i--
    return i
  }
  function draw(index = activeStep()) {
    if (step !== index || elapsed < segmentStart || elapsed >= segmentEnd) {
      step = index
      segmentStart = elapsed
      segmentEnd = Infinity
      // Concurrent steps keep their own packets until their interval ends.
      const visible = []
      for (const s of scene.steps) {
        if (s.at > elapsed) { segmentEnd = Math.min(segmentEnd, s.at); break }
        const end = s.at + s.duration
        if (end > elapsed) { visible.push(s); segmentEnd = Math.min(segmentEnd, end) }
      }
      if (!visible.length && index >= 0) visible.push(scene.steps[index])
      const nextFocus = new Set(visible.flatMap(s => s.focus))
      for (const key of focus) if (!nextFocus.has(key)) items.get(key)?.classList.remove('focus')
      for (const key of nextFocus) {
        if (focus.has(key)) continue
        const item = items.get(key)
        item.classList.add('focus')
        if (playing && !reduced && item.classList.contains('node')) item.animate?.([{opacity:.6},{opacity:1}], {duration:250})
      }
      focus = nextFocus
      const layer = document.createDocumentFragment()
      markers = visible.flatMap(s => s.packets.map(p => {
        const marker = p.marker || 'dot', group = element('g', {'data-marker':marker})
        const symbol = packetShape(marker), route = routes.get(p.edge)
        group.append(symbol, element('text', {class:'packet-label', y:marker === 'dot' ? -10 : -13}, p.label))
        layer.append(group)
        return {group, symbol, route, reverse:p.from !== route.a.id, directional:marker === 'arrow', at:s.at, duration:s.duration, fraction:-1}
      }))
      packetsLayer.replaceChildren(layer)
    }
    for (const marker of markers) {
      const fraction = Math.min(1, Math.max(0, (elapsed - marker.at) / marker.duration))
      if (fraction === marker.fraction) continue
      marker.fraction = fraction
      movePacket(marker, fraction)
    }
  }
  function tick(now) {
    if (!playing) return
    elapsed = Math.min(scene.durationMs, elapsed + (now - previous) * speed)
    previous = now
    const old = step
    draw()
    if (elapsed >= scene.durationMs) {
      state('complete')
      if (!scene.loop || reduced) { pause(); state(); return }
      elapsed = 0
    }
    if (old !== step || now - notified >= 125) { state(); notified = now }
    frame = requestAnimationFrame(tick)
  }
  function init(data) {
    pause()
    scene = data.scene
    reduced = data.reducedMotion === true
    const {width, height, background} = scene.canvas
    scale = Math.min(width / 680, height / 360)
    const pad = 40 * scale
    svg.setAttribute('viewBox', [-pad, -pad, width + 2*pad, height + 2*pad].join(' '))
    svg.setAttribute('aria-labelledby', 'scene-title scene-description')
    svg.dataset.theme = background
    const template = scene.template || 'classic'
    svg.dataset.template = template
    svg.replaceChildren(element('title',{id:'scene-title'},scene.title), element('desc',{id:'scene-description'},scene.accessibility.alt))
    focus.clear()
    const defs = element('defs'), grid = element('pattern', {id:'grid', patternUnits:'userSpaceOnUse', width:24*scale, height:24*scale})
    if (template === 'blueprint') {
      grid.setAttribute('viewBox', '0 0 24 24')
      grid.append(element('path', {class:'grid-line', d:'M0 24V0H24'}))
    } else if (template === 'timeline') {
      grid.setAttribute('viewBox', '0 0 24 24')
      grid.append(element('path', {class:'grid-line', d:'M0 24H24 M0 24v-3'}))
    } else if (template === 'orbit') {
      grid.append(element('circle', {class:'grid', cx:5*scale,cy:7*scale,r:.9*scale}),
        element('circle', {class:'grid', cx:18*scale,cy:19*scale,r:.55*scale}))
    } else grid.append(element('circle', {class:'grid', cx:1, cy:1, r:1.2*scale}))
    defs.append(grid)
    const backdrop = element('rect',{x:-pad,y:-pad,width:width+2*pad,height:height+2*pad,fill:'url(#grid)'})
    const drawing = element('g')
    svg.append(defs, backdrop, drawing)
    positions.clear()
    items.clear()
    routes.clear()
    for (const n of scene.nodes) positions.set(n.id, n)
    for (const e of scene.edges) {
      const a = positions.get(e.from), b = positions.get(e.to)
      const {route, line} = makeRoute(a, b, template)
      routes.set(e.id, route)
      drawing.append(line)
      items.set(e.id, line)
    }
    for (const n of scene.nodes) {
      const group = element('g', {class:'node', 'data-kind':n.kind, transform:'translate('+n.x+' '+n.y+') scale('+scale+')'})
      const chars = Array.from(n.label), label = chars.length > 16 ? chars.slice(0,15).join('')+'…' : n.label
      group.append(element('title', {}, n.label), ...nodeShape(template),
        element('path',{class:'glyph',d:glyph[n.kind]}), element('text',{y:36}, label))
      drawing.append(group)
      items.set(n.id, group)
    }
    if (data.fitContent) {
      // Moving labels can reach either endpoint. Fit once for the whole timeline.
      const box = drawing.getBBox()
      let left = box.x, right = box.x + box.width, top = box.y, bottom = box.y + box.height
      const measure = document.createElement('canvas').getContext('2d')
      if (measure) measure.font = '650 12px system-ui, sans-serif'
      for (const step of scene.steps) for (const packet of step.packets) {
        const a = positions.get(packet.from), b = positions.get(packet.to)
        const half = (measure?.measureText(packet.label).width ?? packet.label.length*14) * scale / 2
        const {ox,oy,radius} = routes.get(packet.edge)
        // The quadratic stays inside the endpoints and its control point.
        const cx = (a.x+b.x)/2+ox, cy = (a.y+b.y)/2+oy
        left = Math.min(left, Math.min(a.x-radius,b.x,cx)-half)
        right = Math.max(right, Math.max(a.x+radius,b.x,cx)+half)
        if (template !== 'classic' || packet.marker && packet.marker !== 'dot') {
          top = Math.min(top, Math.min(a.y-2*radius,b.y,cy)-26*scale)
          bottom = Math.max(bottom, Math.max(a.y,b.y,cy)+12*scale)
        }
      }
      const inset = 12 * scale
      const fitted = [left-inset, top-inset, right-left+2*inset, bottom-top+2*inset]
      svg.setAttribute('viewBox', fitted.join(' '))
      for (const [i, key] of ['x','y','width','height'].entries()) backdrop.setAttribute(key, fitted[i])
    }
    packetsLayer = element('g')
    svg.append(packetsLayer)
    elapsed = Math.max(0, Math.min(scene.durationMs, Number(data.elapsed) || 0))
    speed = [0.5,1,1.5,2].includes(data.speed) ? data.speed : 1
    step = -2
    draw()
    state()
  }
  addEventListener('message', e => {
    const d = e.data
    if (e.source !== parent || !d || d.avanim !== true || d.channel !== channel) return
    if (d.type === 'init') { init(d); return }
    if (!scene) return
    switch (d.type) {
      case 'play':
        if (playing || reduced || document.hidden) return
        if (elapsed >= scene.durationMs) elapsed = 0
        playing = true
        previous = performance.now()
        svg.classList.add('playing')
        frame = requestAnimationFrame(tick)
        break
      case 'pause': pause(); break
      case 'speed': if ([0.5,1,1.5,2].includes(d.value)) speed = d.value; break
      case 'seek':
        pause()
        elapsed = Math.max(0, Math.min(scene.durationMs, Number(d.value) || 0))
        draw()
        break
      case 'step': {
        pause()
        const i = Math.min(scene.steps.length - 1, Math.max(0, step + (d.value < 0 ? -1 : 1)))
        elapsed = scene.steps[i].at
        draw(i)
        break
      }
      default: return
    }
    state()
  })
  document.addEventListener('visibilitychange', () => { if (document.hidden && playing) { pause(); state() } })
  addEventListener('pagehide', pause, {once:true})
  send('ready')
})()
