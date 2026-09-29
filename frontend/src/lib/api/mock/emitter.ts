/** Event names from Guide 6.3. */
export type StreamEventName =
  | 'simulation.tick'
  | 'allocation.status_changed'
  | 'inventory.updated'
  | 'simulator.notice'

export interface StreamEvent {
  name: StreamEventName
  data: unknown
  receivedAt: number
}

type Listener = (event: StreamEvent) => void

interface Subscriber {
  listener: Listener
  maxQueue: number
  queue: StreamEvent[]
  overflowed: boolean
}

const DEFAULT_MAX_QUEUE = 200

/**
 * A stand-in for the simulator's SSE stream.
 *
 * It reproduces the one behaviour from Guide 6.1 that callers must actually
 * handle: a subscriber whose queue exceeds its capacity is silently dropped,
 * and must reconnect and refetch. The other subscribers keep flowing.
 */
export class MockEmitter {
  private subscribers = new Set<Subscriber>()
  private delivering = true

  /** Simulates a stalled consumer without blocking the test thread. */
  pauseDelivery(): void { this.delivering = false }
  resumeDelivery(): void {
    // Re-arm before draining. Draining alone flushes what piled up while
    // paused but leaves `delivering` false, so every event emitted afterwards
    // would queue forever and never reach a listener.
    this.delivering = true
    this.drain()
  }

  subscriberCount(): number { return this.subscribers.size }

  subscribe(listener: Listener, options: { maxQueue?: number } = {}): () => void {
    const subscriber: Subscriber = {
      listener,
      maxQueue: options.maxQueue ?? DEFAULT_MAX_QUEUE,
      queue: [],
      overflowed: false,
    }
    this.subscribers.add(subscriber)
    return () => { this.subscribers.delete(subscriber) }
  }

  emit(name: StreamEventName, data: unknown): void {
    const event: StreamEvent = { name, data, receivedAt: Date.now() }
    for (const subscriber of this.subscribers) {
      subscriber.queue.push(event)
      if (subscriber.queue.length > subscriber.maxQueue) {
        // Guide 6.1: falling behind by more than the queue capacity drops the
        // subscriber. It is removed rather than stalling the emitter.
        subscriber.overflowed = true
      }
    }
    if (this.delivering) this.drain()
  }

  private drain(): void {
    for (const subscriber of [...this.subscribers]) {
      if (subscriber.overflowed) {
        this.subscribers.delete(subscriber)
        continue
      }
      const pending = subscriber.queue
      subscriber.queue = []
      for (const event of pending) {
        try {
          subscriber.listener(event)
        } catch {
          // One misbehaving listener must not stop delivery to the others.
        }
      }
    }
  }
}
