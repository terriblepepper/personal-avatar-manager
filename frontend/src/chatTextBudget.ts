const maxBytes = 64 * 1024
const encoder = new TextEncoder()

export function chatTextFits(value: string): boolean {
  return value.length <= maxBytes / 3 || (value.length <= maxBytes && encoder.encode(value).length <= maxBytes)
}
