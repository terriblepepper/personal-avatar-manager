import { inject, ref, shallowReactive, type InjectionKey } from 'vue'

export function createNotificationInbox() {
  const unread = ref(0)
  const lists = shallowReactive(new Set<() => Promise<void>>())
  let sequence = 0, applied = 0

  function readUnread() {
    const current = ++sequence
    return (count: number) => {
      if (current < applied) return
      applied = current
      unread.value = count
    }
  }
  function refreshLists() { return Promise.all(Array.from(lists, refresh => refresh())) }

  return { unread, lists, readUnread, refreshLists }
}

export const notificationInboxKey: InjectionKey<ReturnType<typeof createNotificationInbox>> = Symbol('notificationInbox')
export const useNotificationInbox = () => inject(notificationInboxKey, createNotificationInbox, true)
