import { describe, it, expect } from 'vitest'
import { createApi } from './index'

describe('createApi', () => {
  it('returns a working mock client in mock mode', async () => {
    const api = createApi('mock')
    await expect(api.getDepots()).resolves.toMatchObject({ data: expect.any(Array) })
  })

  it('returns an http client in live mode', () => {
    const api = createApi('live', 'http://localhost:8000')
    // Distinguishable by the mock-only world handle.
    expect((api as { world?: unknown }).world).toBeUndefined()
  })

  it('treats an unrecognised mode as mock rather than crashing', () => {
    const api = createApi('nonsense' as 'mock')
    expect((api as { world?: unknown }).world).toBeDefined()
  })
})
