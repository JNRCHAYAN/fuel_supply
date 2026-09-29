/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SIMULATOR_MODE?: string
  readonly VITE_SIMULATOR_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
