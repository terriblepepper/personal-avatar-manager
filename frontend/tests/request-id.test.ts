import { afterEach, expect, it, vi } from 'vitest'
import { requestId } from '../src/utils/requestId'
afterEach(() => vi.unstubAllGlobals())
it('generates distinct UUIDs when randomUUID is unavailable on an HTTP deployment', () => {
  const original = globalThis.crypto
  vi.stubGlobal('crypto', { getRandomValues: original.getRandomValues.bind(original) })
  const first = requestId(), second = requestId()
  expect(first).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/)
  expect(second).not.toBe(first)
})
