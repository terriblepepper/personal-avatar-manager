export const MAX_HTML_BYTES = 100 * 1024 * 1024
export function htmlUploadError(files: readonly File[]): string {
  const file = files.find(item => item.size > MAX_HTML_BYTES && /\.html?$/i.test(item.name))
  return file ? `文件「${file.name}」超过 100 MiB` : ''
}
