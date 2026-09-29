import { describe, it, expect, vi } from 'vitest'
import { MockEmitter } from './emitter'

describe('MockEmitter', () => {
  it('delivers events to subscribers in order', () => {
    const emitter = new MockEmitter()
    const seen: string[] = []
    emitter.subscribe((e) => seen.push(e.name))
    emitter.emit('simulation.tick', { tick: 1 })
    emitter.emit('inventory.updated', { entity_id: 'depot-gazipur' })
    expect(seen).toEqual(['simulation.tick', 'inventory.updated'])
  })

  it('stops delivering after unsubscribe', () => {
    const emitter = new MockEmitter()
    const listener = vi.fn()
    const off = emitter.subscribe(listener)
    emitter.emit('simulation.tick', {})
    off()
    emitter.emit('simulation.tick', {})
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('supports several independent subscribers', () => {
    const emitter = new MockEmitter()
    const a = vi.fn()
    const b = vi.fn()
    emitter.subscribe(a)
    emitter.subscribe(b)
    emitter.emit('simulation.tick', {})
    expect(a).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledTimes(1)
  })

  it('drops a subscriber that falls further behind than the queue allows', () => {
    // Guide 6.1: a queue of 200; falling behind by more silently drops you.
    const emitter = new MockEmitter()
    const slow = vi.fn()
    emitter.subscribe(slow, { maxQueue: 3 })

    // Backpressure: the listener cannot drain, so events accumulate.
    emitter.pauseDelivery()
    for (let i = 0; i < 5; i++) emitter.emit('simulation.tick', { tick: i })
    emitter.resumeDelivery()

    expect(slow.mock.calls.length).toBeLessThan(5)
    expect(emitter.subscriberCount()).toBe(0)
  })

  it('keeps other subscribers flowing when one is dropped', () => {
    const emitter = new MockEmitter()
    const slow = vi.fn()
    const fast = vi.fn()
    emitter.subscribe(slow, { maxQueue: 2 })
    emitter.subscribe(fast)

    emitter.pauseDelivery()
    for (let i = 0; i < 6; i++) emitter.emit('simulation.tick', { tick: i })
    emitter.resumeDelivery()

    expect(fast).toHaveBeenCalledTimes(6)
    expect(emitter.subscriberCount()).toBe(1)
  })

  it('never throws when a listener throws', () => {
    const emitter = new MockEmitter()
    const good = vi.fn()
    emitter.subscribe(() => { throw new Error('listener exploded') })
    emitter.subscribe(good)
    expect(() => emitter.emit('simulation.tick', {})).not.toThrow()
    expect(good).toHaveBeenCalledTimes(1)
  })

  it('delivers events emitted after a resume', () => {
    // pauseDelivery and resumeDelivery are a pair. The two tests above resume
    // and then stop, so neither notices if the resume flushes the queue but
    // leaves delivery latched off — every event after it would be swallowed.
    const emitter = new MockEmitter()
    const seen: string[] = []
    emitter.subscribe((e) => seen.push(e.name))

    emitter.pauseDelivery()
    emitter.resumeDelivery()
    emitter.emit('simulation.tick', { tick: 1 })

    expect(seen).toEqual(['simulation.tick'])
  })
})
