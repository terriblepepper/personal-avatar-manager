import type { Message, Task, TaskFile } from '../composables/dialogueTypes'

export function dialogueTranscript(messages: Message[], taskMap: Record<string, Task>) {
  const firstAgentTurn = new Map<string, Map<string | undefined, number>>()
  for (const message of messages) {
    let speakers = firstAgentTurn.get(message.dialogue_id)
    if (!speakers) firstAgentTurn.set(message.dialogue_id, speakers = new Map())
    if (message.speaker_role === 'agent' && !speakers.has(message.speaker_user_id)) speakers.set(message.speaker_user_id, message.turn)
  }
  const filesByDialogue = new Map<string, Map<number, TaskFile[]>>()
  for (const [dialogue, speakers] of firstAgentTurn) {
    const filesByTurn = new Map<number, TaskFile[]>()
    filesByDialogue.set(dialogue, filesByTurn)
    for (const file of taskMap[dialogue]?.files || []) {
      const turn = file.message_id || speakers.get(file.sender_user_id)
      if (turn === undefined) continue
      const files = filesByTurn.get(turn)
      if (files) files.push(file)
      else filesByTurn.set(turn, [file])
    }
  }
  return messages.map((item, index) => {
    const task = taskMap[item.dialogue_id]
    return { item, task, files:filesByDialogue.get(item.dialogue_id)?.get(item.turn),
      starts:messages[index - 1]?.dialogue_id !== item.dialogue_id,
      ends:messages[index + 1]?.dialogue_id !== item.dialogue_id && task?.status === 'ended' }
  })
}
