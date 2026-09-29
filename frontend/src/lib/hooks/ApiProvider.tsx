import { createContext, useContext, type ReactNode } from 'react'
import type { SimulatorClient } from '../api/client'

const ApiContext = createContext<SimulatorClient | null>(null)

export function ApiProvider({
  client, children,
}: { client: SimulatorClient; children: ReactNode }) {
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>
}

export function useApi(): SimulatorClient {
  const client = useContext(ApiContext)
  if (!client) throw new Error('useApi must be used inside an ApiProvider')
  return client
}
