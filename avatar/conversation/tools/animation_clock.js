// Render from absolute time so playback, keyboard seeking and dragging show the same frame.
function installAnimationClock(host, duration, renderFrame) {
  const root = host.attachShadow({mode: 'open'})
  root.innerHTML = `<style>
    :host{display:block;color-scheme:dark}
    .bar{display:flex;align-items:center;gap:.7rem;padding:.45rem .65rem;border-radius:.65rem;background:#16283e;color:#eff6ff;font:14px/1.4 system-ui,sans-serif}
    button{flex:none;min-height:36px;padding:0 .8rem;background:#203752;color:inherit;border:1px solid #6684a0;border-radius:.4rem;cursor:pointer}
    input{flex:1;min-width:0;accent-color:#65d5ff}output{font-variant-numeric:tabular-nums}
    :focus-visible{outline:2px solid #ffd47d;outline-offset:2px}
  </style><div class="bar" role="group" aria-label="动画播放控制"><button type="button">播放</button>
    <input aria-label="播放进度" type="range" min="0" max="1000" step="1" value="0"><output aria-hidden="true"></output></div>`
  const button = root.querySelector('button'), slider = root.querySelector('input'), label = root.querySelector('output')
  const state = {}
  let time = 0, previous, frame = 0, seeking = false
  let playing = !matchMedia('(prefers-reduced-motion: reduce)').matches
  function draw() {
    try {
      renderFrame(time, duration, state)
    } catch {
      playing = false
      button.textContent = '重试播放'
      label.removeAttribute('aria-hidden')
      label.setAttribute('role', 'alert')
      label.textContent = '动画帧绘制失败'
      return
    }
    if (label.hasAttribute('role')) {
      label.removeAttribute('role')
      label.setAttribute('aria-hidden', 'true')
    }
    slider.value = Math.round(time / duration * 1000)
    const elapsed = time.toFixed(1) + ' / ' + duration + ' 秒'
    label.textContent = elapsed
    slider.setAttribute('aria-valuetext', elapsed)
    button.textContent = playing ? '暂停' : '播放'
  }
  function tick(now) {
    frame = 0
    if (previous !== undefined) time = (time + (now - previous) / 1000) % duration
    previous = now
    draw()
    schedule()
  }
  function schedule() {
    if (playing && !seeking && !document.hidden && !frame) frame = requestAnimationFrame(tick)
  }
  function sync() {
    if (frame) cancelAnimationFrame(frame)
    frame = 0
    previous = undefined
    draw()
    schedule()
  }
  button.onclick = () => { playing = !playing; sync() }
  slider.onpointerdown = () => { seeking = true; sync() }
  function endSeek() { if (seeking) { seeking = false; sync() } }
  window.addEventListener('pointerup', endSeek)
  window.addEventListener('pointercancel', endSeek)
  window.addEventListener('blur', endSeek)
  slider.oninput = () => { time = Number(slider.value) / 1000 * duration; previous = undefined; draw() }
  document.addEventListener('visibilitychange', sync)
  sync()
  return draw
}
