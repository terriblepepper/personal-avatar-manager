import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'
import DialoguesView from '../src/views/DialoguesView.vue'

const request = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', () => ({request, notifyError: vi.fn()}))
vi.mock('vue-router', () => ({
  useRoute: () => ({query: {session: 'session-1'}}),
  useRouter: () => ({replace: vi.fn()}),
}))

it.each([undefined, ''])('discards previous messages when history revision is %s', async revision => {
  let first = 1
  request.mockImplementation(async (path: string) => {
    if (path === '/me/skills') return []
    if (path === '/agent-dialogue-settings') return {max_rounds: 10}
    if (path !== '/agent-sessions/session-1') return {items: [], total: 0, offset: 0}
    const messages = Array.from({length: 10}, (_, i) => ({
      turn: i + first, dialogue_id: 'chat', speaker_name: 'Peer', speaker_role: 'agent', content: String(i + first),
    }))
    return {peer_name: 'Peer', resource_conversation_id: '', messages, has_more: true,
      ...(revision === undefined ? {} : {history_revision: revision})}
  })
  const view = mount(DialoguesView, {global: {stubs: {Teleport: true, ChatFilesManager: true,
    ConversationTools: true, ChatMarkdown: {props: ['source'], template: '<p>{{source}}</p>'}}}})
  const turns = () => view.findAll('.peer-message p').map(node => Number(node.text()))
  try {
    await flushPromises()
    expect(turns()).toContain(1)
    first = 2
    window.dispatchEvent(new Event('online'))
    await flushPromises()
    expect(turns()).toEqual([2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
  } finally { view.unmount() }
})
