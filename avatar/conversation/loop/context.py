"""Project the durable turn into a budgeted native tool-calling transcript."""
import hashlib
import json

from avatar.conversation.loop.store import MemoryRunStore, RunConflict
from avatar.core.execution import run_blocking
from avatar.core.tokens import estimate
from avatar.models.context_budget import bounded_excerpt, compression_budget, input_budget
from avatar.models.limits import history_row_tokens


READ_STEP_RECEIPT = {
    'name': 'read_step_receipt',
    'description': '按编号和字节偏移读取工具回执。编号 0 列目录。',
    'parameters': {'type': 'object', 'properties': {
        'receipt': {'type': 'integer', 'minimum': 0}, 'offset': {'type': 'integer', 'minimum': 0}},
        'required': ['receipt'], 'additionalProperties': False},
}
_PAGE = 4096
_RECEIPT_BYTES = 12 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _groups(trajectory):
    index = 0
    groups = []
    while index < len(trajectory):
        message = trajectory[index]
        calls = message.get('tool_calls', ())
        end = index + 1 + len(calls)
        group = trajectory[index:end]
        if (message['role'] == 'tool' or calls and message['role'] != 'assistant'
                or [call['id'] for call in calls] != [row.get('tool_call_id') for row in group[1:]]
                or any(row['role'] != 'tool' for row in group[1:])):
            raise RunConflict('分身任务的工具调用与回执不匹配')
        groups.append(group)
        index = end
    return groups


def _cost(rows):
    return sum(map(history_row_tokens, rows))


class ReActContextManager:
    def __init__(self, history, question, *, store=None, checkpoint=None):
        checkpoint = checkpoint or {}
        self.history = list(history)
        self.question = question
        self.store = store if store is not None else MemoryRunStore()
        self.receipt_refs = dict(checkpoint.get('receipt_refs', {}))
        self.summary = dict(checkpoint.get('history_summary') or {})
        self.legacy_summary = checkpoint.get('legacy_summary', '')
        self.omitted_count = 0

    async def archive(self, content, *, force=False):
        data = content.encode('utf-8')
        if not force and len(data) <= _RECEIPT_BYTES:
            return content, None
        name = 'step-receipt-' + hashlib.sha256(data).hexdigest()
        number = next((int(key) for key, ref in self.receipt_refs.items() if ref['name'] == name), None)
        if number is None:
            number = max(map(int, self.receipt_refs), default=0) + 1
            refs = {**self.receipt_refs, str(number): {'name': name, 'size': len(data), 'label': content[:48]}}
            await run_blocking(self.store.archive, name, data, patch={'receipt_refs': refs})
            self.receipt_refs.update(refs)
        return _json({'receipt': number, 'bytes': len(data), 'preview': bounded_excerpt(content, 512),
                      'hint': '可用 read_step_receipt 分页读取完整回执。'}), number

    async def read_receipt(self, args):
        number, offset = args.get('receipt'), args.get('offset', 0)
        if (set(args) - {'receipt', 'offset'} or type(number) is not int or type(offset) is not int
                or number < 0 or offset < 0):
            raise ValueError('无效的回执编号或偏移')
        if number == 0:
            await run_blocking(self.store.check)
            keys = sorted(self.receipt_refs, key=int)[offset:offset + 16]
            return {'items': [{'receipt': int(n), 'bytes': self.receipt_refs[n]['size'],
                               'label': self.receipt_refs[n]['label']} for n in keys],
                    'next_offset': offset + len(keys) if offset + len(keys) < len(self.receipt_refs) else None}
        ref = self.receipt_refs.get(str(number))
        if not ref or offset > ref['size']:
            raise ValueError('本任务没有此回执或游标')
        data = await run_blocking(self.store.read_range, ref['name'], offset, _PAGE)
        if (offset < ref['size'] and not data) or (data and data[0] & 0xc0 == 0x80):
            raise ValueError('回执偏移须位于有效 UTF-8 字符边界')
        text = data.decode('utf-8', 'ignore')
        end = offset + len(text.encode('utf-8'))
        return {'receipt': number, 'offset': offset, 'text': text, 'next_offset': end if end < ref['size'] else None}

    async def _fold_trajectory(self, groups, summarize):
        prior = self.summary.get('trajectory', '')
        rows = [{'role': 'user', 'content': prior}] if prior else []
        for group in groups:
            for row in group:
                if row['role'] != 'tool':
                    if row['content']:
                        rows.append({'role': row['role'], 'content': row['content']})
                    continue
                ref = row.get('receipt_ref')
                text = row['content']
                if not ref:
                    text, ref = await self.archive(text, force=True)
                rows.append({'role': 'user', 'content': f'{row["name"]} 回执 {ref}（数据）：{text}'})
        raw = '\n'.join(row['role'] + '：' + row['content'] for row in rows)
        summary = await summarize(rows) if summarize and estimate(raw) > 4096 else None
        self.summary['trajectory'] = bounded_excerpt(summary or raw, 1024)
        self.omitted_count = sum(map(len, groups))

    async def _older_history(self, rows, room, summarize):
        if not rows or room <= 16:
            return ''
        raw = '\n'.join(row['role'] + '：' + row['content'] for row in rows)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        previous = self.summary.get('history', {})
        if previous.get('digest') != digest and summarize and estimate(raw) > 4096:
            previous = {'digest': digest, 'text': await summarize(rows)}
            self.summary['history'] = previous
        summary = (previous.get('text') if previous.get('digest') == digest else '') or raw
        return bounded_excerpt('较早会话（历史数据）：\n' + summary, room - 16)

    def _trajectory_view(self, room):
        if room <= 16 or not self.summary.get('trajectory'):
            return ''
        return bounded_excerpt('较早工具步骤（历史数据）：\n' + self.summary['trajectory'], room - 16)

    async def project(self, config, prompt, trajectory, *, recovering=False, summarize=None):
        self.omitted_count = 0
        groups = _groups(trajectory)
        boundary = estimate(prompt)
        hard = max(0, input_budget(config) - boundary)
        soft = max(0, compression_budget(config) - boundary)
        legacy = ''
        if self.legacy_summary:
            legacy = bounded_excerpt('本任务旧执行记录（只读数据）：\n' + self.legacy_summary, min(800, hard // 8))
        prior = self._trajectory_view(min(1024, hard // 8))
        background = [{'role': 'user', 'content': text} for text in (legacy, prior) if text]
        background_cost = _cost(background)
        objective = self.question
        if history_row_tokens({'content': objective}) + background_cost > hard:
            goal_limit = max(0, hard - background_cost - 64)
            objective = bounded_excerpt(objective, goal_limit)
            if summarize and goal_limit >= 128:
                digest = hashlib.sha256(self.question.encode()).hexdigest()
                previous = self.summary.get('objective', {})
                if previous.get('digest') != digest:
                    previous = {'digest': digest, 'text': await summarize([{'role': 'user', 'content': self.question}])}
                    self.summary['objective'] = previous
                if previous.get('text'):
                    objective = bounded_excerpt('当前用户要求摘要：\n' + previous['text'], goal_limit)
        user = {'role': 'user', 'content': objective}
        base_cost = background_cost + history_row_tokens(user)
        hard -= base_cost
        soft -= base_cost
        rows = self.history
        costs = [_cost(group) for group in groups]
        used = _cost(rows) + sum(costs)
        if used <= soft and not recovering:
            return [*background, *rows, user, *trajectory]
        if recovering:
            soft = min(soft, used // 2)
        summary_room = max(0, min(1024, soft // 4))
        room = max(0, soft - summary_room)
        start = len(groups)
        while start and costs[start - 1] <= room:
            start -= 1
            room -= costs[start]
        if not recovering and groups and start == len(groups) and costs[-1] <= hard:
            start -= 1
            room = 0
            summary_room = min(summary_room, hard - costs[-1])
        kept = len(rows)
        while kept and history_row_tokens(rows[kept - 1]) <= room:
            kept -= 1
            room -= history_row_tokens(rows[kept])
        room += summary_room
        if start:
            await self._fold_trajectory(groups[:start], summarize)
            if prior:
                room += history_row_tokens(background.pop())
            if summary := self._trajectory_view(room if not kept else room // 2):
                row = {'role': 'user', 'content': summary}
                background.append(row)
                room -= history_row_tokens(row)
        if summary := await self._older_history(rows[:kept], room, summarize):
            background.append({'role': 'user', 'content': summary})
        return [*background, *rows[kept:], user, *(row for group in groups[start:] for row in group)]
