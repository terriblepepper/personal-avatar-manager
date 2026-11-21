export const TEXT_PREVIEW_LIMIT = 1024 * 1024
export function previewKind(ext: string) {
  if (ext === 'avanim') return 'animation'
  if (ext === 'mmd') return 'mermaid'
  if (ext === 'html' || ext === 'htm') return 'html'
  if (ext === 'pdf') return 'pdf'
  if (['png','jpg','jpeg','webp','gif','bmp'].includes(ext)) return 'image'
  if (['mp3','wav','m4a','ogg','flac'].includes(ext)) return 'audio'
  if (['mp4','mov','webm'].includes(ext)) return 'video'
  if (['md','markdown','txt','csv','tsv','json','log','yaml','yml','toml','ini','py','js','ts','css','xml','sh','sql','svg'].includes(ext)) return 'text'
  return ''
}

export async function readPreviewBytes(url: string, limit: number, signal: AbortSignal) {
  const response = await fetch(url, {credentials:'same-origin', redirect:'error', cache:'no-store', signal})
  if (!response.ok) {
    if (response.status === 401) throw Error('登录已失效，请重新登录')
    if ([403,404].includes(response.status)) throw Error('文件不存在或没有访问权限')
    throw Error('无法预览文件，可下载原件查看')
  }
  const reader = response.body?.getReader()
  if (!reader) throw Error('无法读取文件')
  const chunks: Uint8Array<ArrayBuffer>[] = []
  let size = 0
  try {
    if (!response.headers.get('content-encoding') && Number(response.headers.get('content-length')) > limit) throw Error('文件超出预览大小限制，请下载原件')
    while (true) {
      const {done,value} = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > limit) throw Error('文件超出预览大小限制，请下载原件')
      chunks.push(new Uint8Array(value))
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
  if (chunks.length === 1) return chunks[0]!
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk,offset); offset += chunk.length }
  return bytes
}

export function decodePreviewText(bytes: Uint8Array) {
  try {
    const encoding = bytes[0] === 255 && bytes[1] === 254 ? 'utf-16le'
      : bytes[0] === 254 && bytes[1] === 255 ? 'utf-16be' : 'utf-8'
    return new TextDecoder(encoding,{fatal:true}).decode(bytes)
  } catch { return new TextDecoder('gb18030').decode(bytes) }
}
