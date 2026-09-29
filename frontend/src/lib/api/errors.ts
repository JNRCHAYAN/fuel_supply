import type { SimulatorErrorCode } from '../types/simulator'

/**
 * Raised by both client implementations so callers handle one error shape.
 *
 * The Guide uses three different envelopes (9): allocation failures arrive as
 * {"detail":{"code","message"}}, injected faults as
 * {"error":{"code","message"}}, and Pydantic validation as FastAPI's default
 * {"detail":[...]}. All three normalise into this type at the boundary.
 */
export class SimulatorError extends Error {
  readonly status: number
  readonly code: SimulatorErrorCode | string

  constructor(status: number, code: SimulatorErrorCode | string, message: string) {
    super(message)
    this.name = 'SimulatorError'
    this.status = status
    this.code = code
  }
}
