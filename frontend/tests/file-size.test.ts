import { expect, it } from 'vitest'
import { fileSizeMB } from '../src/utils/format'

it('formats decimal megabytes and keeps nonempty small files distinct from empty ones', () => {
  expect([0, 1, 9999, 10000, 1250000].map(fileSizeMB)).toEqual(['0 MB', '<0.01 MB', '<0.01 MB', '0.01 MB', '1.25 MB'])
})
