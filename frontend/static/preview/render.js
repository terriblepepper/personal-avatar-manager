/* 仅在 opaque-origin iframe 中运行；不访问父页面、Cookie、存储、数据库或网络。 */
(() => {
  const output = document.querySelector('#output'), origin = new URL(location.href).origin;
  let channel, source, blocked = 0, busy = false;
  const notify = (type, data = {}) => parent.postMessage({ mermaidSandbox: true, channel, type, ...data }, origin);
  const raster = value => /^data:image\/(?:png|jpeg|gif|webp);base64,[a-z\d+/=]+$/i.test(value);
  function css(value) {
    return value.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\\([\da-f]{1,6})\s?|\\(.)/gi, (_, hex, char) => hex ? String.fromCodePoint(Math.min(parseInt(hex, 16), 0x10ffff)) : char);
  }
  const unsafe = value => /@import|expression\s*\(|(?:https?:|javascript:|file:|\/\/)|url\s*\((?!\s*['"]?#[\w:.-]+['"]?\s*\))/i.test(css(value));
  // HTML 标签在隔离页中清理；资源先替换为 data PNG，导出的 SVG 不含主动内容或远程请求。
  function clean(value, resources) {
    const safe = DOMPurify.sanitize(value, { USE_PROFILES: { html: true, svg: true, svgFilters: true }, ADD_TAGS: ['foreignObject', 'style'], HTML_INTEGRATION_POINTS: { foreignobject: true }, FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'video', 'audio', 'link', 'meta', 'base'], ALLOW_DATA_ATTR: false });
    const host = document.createElement('div');
    host.innerHTML = safe;
    for (const node of host.querySelectorAll('*')) {
      for (const attr of [...node.attributes]) {
        if (/^on/i.test(attr.name))
          node.removeAttribute(attr.name);
        if (['src', 'href', 'xlink:href'].includes(attr.name)) {
          const v = attr.value.trim(), tag = node.tagName.toLowerCase();
          if ((tag === 'img' && attr.name === 'src') || (tag === 'image' && attr.name !== 'src')) {
            if (resources[v] && raster(resources[v]))
              node.setAttribute(attr.name, resources[v]);
            else if (!raster(v)) {
              node.removeAttribute(attr.name);
              if (v && !v.startsWith('#'))
                blocked++;
            }
          }
          else if (!v.startsWith('#') && !(tag === 'a' && /^https?:\/\//i.test(v))) {
            node.removeAttribute(attr.name);
          }
        }
        if ((attr.name === 'style' || /url\s*\(/i.test(attr.value)) && unsafe(attr.value))
          node.removeAttribute(attr.name);
      }
      if (node.tagName.toLowerCase() === 'style' && unsafe(node.textContent))
        node.remove();
      if (node.tagName.toLowerCase() === 'a') {
        node.removeAttribute('target');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    }
    const svg = host.querySelector('svg');
    if (!svg)
      throw Error('图形未生成有效 SVG');
    svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    // XML 序列化保留 HTML 命名空间并闭合 br 等标签，浏览器才能将 SVG 解码为 PNG。
    return new XMLSerializer().serializeToString(svg);
  }
  // 捕获链接/回调手势，只向父页报告公开事件；绝不调用模型指定的 window 函数。
  output.addEventListener('click', event => {
    const link = event.target.closest('a'), node = event.target.closest('.clickable');
    if (!link && !node) {
      if (event.target.closest('svg')) notify('preview');
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    if (link) {
      notify('link', { url: link.getAttribute('href') || link.getAttribute('xlink:href') || '' });
      return;
    }
    const id = node.dataset.id || node.id || '', match = [...source.matchAll(/^\s*click\s+(\S+)\s+(?:(?:call|callback)\s+)?([\w.]+)(?:\(\))?/gm)].find(m => id === m[1] || id.includes('-flowchart-' + m[1] + '-'));
    notify('callback', { name: match?.[2] || 'click', node: id });
  }, true);
  addEventListener('message', async (event) => {
    if (event.source !== parent || event.origin !== origin || event.data?.type !== 'render' || busy)
      return;
    const blobs = [];
    const data = event.data;
    busy = true;
    channel = data.channel;
    source = data.code;
    blocked = 0;
    try {
      if (typeof source !== 'string' || source.length > 100000)
        throw Error('图表超过 100000 字符');
      document.body.style.fontFamily = String(data.font || 'sans-serif');
      const resources = { ...(data.resources || {}) };
      const imageUrls = [...new Set([...source.matchAll(/(?:\bimg\s*:\s*["']|<img\b[^>]*\bsrc\s*=\s*["'])(https:\/\/[^"'<>\s]+)["']/gi)].map(m => m[1]))];
      if (imageUrls.length > 5)
        throw Error('每幅图最多使用 5 张外部图片');
      // 图片排版会先读取尺寸：未授权资源用本地占位 PNG，避免 Mermaid 尝试联网或整图失败。
      const canvas = document.createElement('canvas');
      canvas.width = 80;
      canvas.height = 48;
      const paint = canvas.getContext('2d');
      paint.fillStyle = '#edf1ee';
      paint.fillRect(0, 0, 80, 48);
      paint.fillStyle = '#576b61';
      paint.font = '12px sans-serif';
      paint.fillText('图片待加载', 8, 28);
      const placeholder = canvas.toDataURL('image/png');
      for (const url of imageUrls)
        if (!raster(resources[url] || '')) {
          resources[url] = placeholder;
          blocked++;
        }
      if (Object.values(resources).reduce((n, value) => n + String(value).length, 0) > 4 * 1024 * 1024)
        throw Error('嵌入图片超过 4 MiB，请缩小图片');
      let renderCode = source;
      for (const [url, value] of Object.entries(resources))
        if (raster(value)) {
          const bytes = Uint8Array.from(atob(value.split(',')[1]), c => c.charCodeAt(0));
          const blob = URL.createObjectURL(new Blob([bytes], { type: 'image/png' }));
          blobs.push(blob);
          resources[blob] = value;
          renderCode = renderCode.split(url).join(blob);
        }
      mermaid.initialize({ startOnLoad: false, securityLevel: 'loose', suppressErrorRendering: true, maxTextSize: 100000, maxEdges: 500,
        secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges', 'suppressErrorRendering', 'dompurifyConfig'],
        fontFamily: document.body.style.fontFamily, theme: 'default', htmlLabels: true,
        dompurifyConfig: { ALLOWED_URI_REGEXP: /^(?:(?:https?|blob):|data:image\/(?:png|jpeg|gif|webp);base64,|#|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i } });
      const packs = Object.create(null);
      for (const match of source.matchAll(/\b([a-z][a-z0-9-]{0,39}):([a-z0-9][a-z0-9-]{0,79})\b/g)) {
        if (match[1] === 'https' || match[1] === 'http')
          continue;
        (packs[match[1]] ||= new Set()).add(match[2]);
      }
      const iconData = data.icons || {};
      mermaid.registerIconPacks(Object.entries(packs).map(([name, names]) => ({ name, loader: async () => {
          if (iconData[name])
            return iconData[name];
          blocked += names.size;
          return { prefix: name, width: 24, height: 24, icons: Object.fromEntries([...names].map(icon => [icon, { body: '<rect x="2" y="2" width="20" height="20" fill="none" stroke="currentColor"/><text x="12" y="17" text-anchor="middle" font-size="16">?</text>' }])) };
        } })));
      const result = await mermaid.render('diagram', renderCode, output);
      if (result.svg.length > 4 * 1024 * 1024)
        throw Error('图形超过 4 MiB，请拆分');
      let raw = result.svg;
      for (const blob of blobs)
        raw = raw.split(blob).join(resources[blob]);
      const svg = clean(raw, resources);
      if (svg.length > 4 * 1024 * 1024)
        throw Error('含资源的图形超过 4 MiB，请缩小图片或拆分');
      output.innerHTML = svg;
      // 使用原生 SVG title 保留 click 的说明；不安装任意命名函数的执行绑定。
      for (const match of source.matchAll(/^\s*click\s+(\S+)[^\n]*["']([^"']+)["']\s*$/gm)) {
        const node = [...output.querySelectorAll('.clickable')].find(n => n.id.includes('-flowchart-' + match[1] + '-'));
        if (node) {
          const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
          title.textContent = match[2];
          node.prepend(title);
        }
      }
      // 不绑定 Mermaid 任意命名回调；HTML/SVG 自身的 title 和链接手势仍可使用。
      const drawing = output.querySelector('svg');
      const box = drawing.viewBox.baseVal;
      notify('complete', { svg, blocked, width: box.width || drawing.clientWidth, height: box.height || drawing.clientHeight });
    }
    catch (error) {
      notify('error', { message: String(error.message || error).slice(0, 500) });
    }
    finally {
      for (const blob of blobs)
        URL.revokeObjectURL(blob);
      busy = false;
    }
  });
  parent.postMessage({ mermaidSandbox: true, type: 'ready' }, origin);
})();
