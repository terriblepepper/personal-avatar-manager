export const SOUL_ACCEPT = '.png,.jpg,.jpeg,.webp,.gif,.bmp,.md,.txt,.doc,.docx'
const suffixes = new Set(SOUL_ACCEPT.split(','))

export function supportsSoulAsset(filename: string) {
  return suffixes.has('.' + filename.toLowerCase().split('.').at(-1)) && filename.lastIndexOf('.') > 0
}
