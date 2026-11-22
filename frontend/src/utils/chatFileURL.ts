import type { ChatFile } from '../types'

export function chatFileURL(file: ChatFile, conversationId?: string) {
  if (file.expired || !/^[\w-]+$/.test(file.id)) return ''
  if (file.origin === 'material') {
    const original = '/api/me/attachments/' + file.id
    return file.download_url === original ? original : ''
  }
  const url = file.download_url || (conversationId
    ? '/api/conversations/'+encodeURIComponent(conversationId)+'/files/'+file.id : '')
  return /^\/api\/conversations\/[\w-]+\/files\/[\w-]+$/.test(url) && url.endsWith('/'+file.id) ? url : ''
}

// Older source references carried only a server-supplied, same-origin API download URL.
export function chatPreviewURL(file: ChatFile, conversationId?: string) {
  if (file.expired || file.origin || !file.download_url) return chatFileURL(file, conversationId)
  try {
    const url = new URL(file.download_url, location.href)
    return url.origin === location.origin && url.pathname.startsWith('/api/') && !url.username && !url.password
      ? url.pathname + url.search : ''
  } catch { return '' }
}
