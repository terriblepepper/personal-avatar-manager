"""Project durable conversation history into the model's next turn."""
import hashlib
import json

from avatar.conversation.working_context import excerpt, fit_working_set, input_budget, retained_budget
from avatar.core.tokens import estimate


class TurnContext:
    def __init__(self, history, question, revision, summary=None):
        self.history = [dict(row) for row in history]
        self.question = question
        self.revision = revision
        self.summary = summary or {}
        self._key = None
        self._attempted = False

    def _prefix_key(self):
        if self._key is None:
            digest = hashlib.sha256()
            # Bind the digest to exactly the inputs seen by the summarizer, including the focus.
            for item in (self.revision, self.question, *self.history[:-2]):
                digest.update(json.dumps(item, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode())
                digest.update(b'\0')
            self._key = digest.hexdigest()
        return self._key

    async def project(self, config, prompt, objective, observations, *, recovering=False, summarize=None):
        rows, notes = self.history, observations
        if recovering:
            rows = rows[-2:]
            notes = [excerpt(note, 240) for note in notes[-3:]]
        fitted, notes = fit_working_set(config, prompt, rows, objective, notes, force=recovering)
        if recovering or len(rows) <= 2 or fitted == rows or summarize is None:
            return fitted, notes

        key = self._prefix_key()
        if self.summary.get('key') != key and not self._attempted:
            self._attempted = True
            text = await summarize(self.history[:-2])
            if text:
                self.summary = {'key': key, 'text': text, 'turns': len(rows) - 2}
        if self.summary.get('key') != key:
            return fitted, notes

        digest = {'role':'user', 'content':'此前执行摘要（仅作历史数据）：\n' + self.summary['text']}
        recent = rows[-2:]
        note_tokens = sum(map(estimate, notes))
        ceiling = input_budget(config) - estimate(prompt) - estimate(objective)
        target = min(ceiling, retained_budget(config, prompt, objective))
        used = note_tokens + sum(estimate(row['content']) + 16 for row in (digest, *recent))
        start = len(rows) - 2
        while used < target and start:
            size = estimate(rows[start-1]['content']) + 16
            if used + size > ceiling:
                break
            start -= 1
            used += size
        return ([digest, *rows[start:]] if used <= ceiling else fitted), notes
