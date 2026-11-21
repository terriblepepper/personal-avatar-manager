import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { expect, it, vi } from 'vitest'
import { usePolling } from '../src/composables/usePolling'

it('coalesces reads, refreshes after an in-flight change and resumes once after visibility returns', async () => {
  vi.useFakeTimers({toFake:['setTimeout','clearTimeout']})
  const hidden = vi.spyOn(document,'hidden','get').mockReturnValue(false)
  const calls: AbortSignal[] = []
  let complete!: () => void
  const read = (signal:AbortSignal) => {
    calls.push(signal)
    return new Promise<void>(resolve => { complete = resolve })
  }
  let refresh!: () => Promise<void>
  const view = mount(defineComponent({setup() { ({refresh} = usePolling(read, () => 30000)); return () => null }}))
  try {
    await flushPromises()
    void refresh(); void refresh()
    await vi.advanceTimersByTimeAsync(60000)
    expect(calls).toHaveLength(1)
    complete(); await flushPromises()
    expect(calls).toHaveLength(2)
    hidden.mockReturnValue(true)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(calls[1].aborted).toBe(true)
    complete(); await vi.advanceTimersByTimeAsync(60000)
    expect(calls).toHaveLength(2)
    hidden.mockReturnValue(false)
    document.dispatchEvent(new Event('visibilitychange')); window.dispatchEvent(new Event('online'))
    await flushPromises()
    expect(calls).toHaveLength(3)
    complete(); await flushPromises()
    await vi.advanceTimersByTimeAsync(29999)
    expect(calls).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(4)
  } finally { view.unmount(); complete?.(); hidden.mockRestore(); vi.useRealTimers() }
})
