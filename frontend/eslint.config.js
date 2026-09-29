import tseslint from '@typescript-eslint/eslint-plugin'
import tsparser from '@typescript-eslint/parser'
import reactHooks from 'eslint-plugin-react-hooks'

// The layering rule from spec 4.2: feature code must never reach past the
// client interface into fixtures or the mock implementation. Without this it
// erodes silently and the mock-to-live swap stops being a config change.
const LAYERING = [
  {
    group: ['**/api/mock/*', '**/api/mock/**', '**/fixtures', '**/fixtures/**'],
    message:
      'Features must not import fixtures or the mock client. Import from lib/api instead.',
  },
]

export default [
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  {
    files: ['**/*.ts', '**/*.tsx'],
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: 2022, sourceType: 'module', ecmaFeatures: { jsx: true } },
    },
    plugins: { '@typescript-eslint': tseslint, 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    // Scoped to src/features on purpose. lib/api/index.ts is the one file that
    // must import the mock client — that is the whole point of the swappable
    // client — so a blanket rule would forbid the selector it exists to serve.
    files: ['src/features/**/*.ts', 'src/features/**/*.tsx'],
    rules: {
      'no-restricted-imports': ['error', { patterns: LAYERING }],
    },
  },
]
