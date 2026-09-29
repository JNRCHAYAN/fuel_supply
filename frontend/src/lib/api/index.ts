import type { SimulatorClient } from './client'
import { createMockClient, type MockClient } from './mock/mockClient'
import { createHttpClient } from './http/httpClient'

export type SimulatorMode = 'mock' | 'live'

const DEFAULT_BASE_URL = 'http://localhost:8000'

export function createApi(mode: SimulatorMode | string, baseUrl?: string): SimulatorClient {
  // An unrecognised mode falls back to mock rather than throwing: a typo in an
  // environment variable must not leave the operator with a blank console.
  if (mode === 'live') {
    return createHttpClient({ baseUrl: baseUrl ?? DEFAULT_BASE_URL })
  }
  return createMockClient()
}

export const SIMULATOR_MODE: SimulatorMode =
  import.meta.env.VITE_SIMULATOR_MODE === 'live' ? 'live' : 'mock'

export const api: SimulatorClient = createApi(
  SIMULATOR_MODE,
  import.meta.env.VITE_SIMULATOR_BASE_URL,
)

/** Narrowing helper for the Scenario and Resilience screens, which need the world. */
export function asMockClient(client: SimulatorClient): MockClient | null {
  return (client as MockClient).world ? (client as MockClient) : null
}

export type { SimulatorClient, SimResponse, StreamHandle } from './client'
export { SimulatorError } from './errors'
