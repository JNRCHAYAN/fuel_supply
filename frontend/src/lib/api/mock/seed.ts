/**
 * mulberry32 — a small, fast, deterministic PRNG.
 *
 * The Guide states the world is deterministic: same scenario + same seed +
 * same actions produce byte-identical state. A seeded generator is what makes
 * that true in the browser, and what makes a demo reproducible.
 */
export function createRng(seed: number): () => number {
  let state = seed >>> 0
  return function next(): number {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
