import { request } from '../api/client'
import type { FilePromotion } from '../types'

interface AnimationReceipt { attachment_id: string | null; already_exists: boolean }
async function animationRequest(conversationId: string, action: 'lookup' | 'promote', body: object, signal: AbortSignal): Promise<AnimationReceipt> {
  const path = '/conversations/' + encodeURIComponent(conversationId) + '/animations/' + action
  let result: Partial<AnimationReceipt> | null
  try {
    result = await request<Partial<AnimationReceipt> | null>(path, 'POST', body, signal)
  } catch (error) {
    if (signal.aborted) throw error
    const status = error instanceof Error && 'status' in error ? Number(error.status) : 0
    if (status >= 400 && status < 500 && status !== 408) throw error
    throw Error('转存状态未知，请重新检查', { cause: error })
  }
  const id = typeof result?.attachment_id === 'string' && result.attachment_id.trim() || null
  if (result?.already_exists === true || id) return { attachment_id: id, already_exists: result?.already_exists === true }
  if (action === 'lookup' && result?.already_exists === false && result.attachment_id === null) return { attachment_id: null, already_exists: false }
  throw Error('转存状态未知，请重新检查')
}
export const submitAnimationPromotion = (conversationId: string, source: string, value: FilePromotion, signal: AbortSignal) =>
  animationRequest(conversationId, 'promote', { source, ...value }, signal)
export const readAnimationPromotion = (conversationId: string, source: string, signal: AbortSignal) =>
  animationRequest(conversationId, 'lookup', { source }, signal)
