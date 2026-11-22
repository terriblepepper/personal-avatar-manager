"""Project the durable transcript and the current tool turn into the next model request."""
import asyncio
import hashlib
import json
from itertools import islice

from avatar.conversation.loop.store import RunCapacity, draft_byte_limit
from avatar.conversation.working_context import bounded_excerpt, excerpt, fit_working_set, input_budget, retained_budget
from avatar.core.execution import run_blocking
from avatar.core.tokens import estimate
from avatar.models.limits import tool_budget


READ_STEP_RECEIPT = {
    'name':'read_step_receipt',
    'description':'在本任务内分页读取已压缩的真实工具回执。receipt=0 列目录，其他编号按 UTF-8 字节 offset 读取。',
    'parameters':{'type':'object','properties':{
        'receipt':{'type':'integer','minimum':0},'offset':{'type':'integer','minimum':0}},
        'required':['receipt'],'additionalProperties':False},
}
_RECENT = 12
_PAGE = 4096


class TurnContext:
    def __init__(self, history, question, revision, summary=None, *, store=None, checkpoint=None):
        self.history = [dict(row) for row in history]
        self.question = question
        self.revision = revision
        self.summary = dict(summary or {})
        self.store = store
        checkpoint = checkpoint or {}
        self.base = checkpoint.get('observation_base', 0)
        self.refs = dict(checkpoint.get('observation_refs', {}))
        self.scanned = 0
        self.pages = dict(checkpoint.get('observation_pages', {}))
        self._page_views = {}
        self._visible_pages = set()
        self._key = None
        self._attempted = set()

    def _prefix_key(self):
        if self._key is None:
            digest = hashlib.sha256()
            for item in (self.revision, self.question, *self.history[:-2]):
                digest.update(json.dumps(item, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode())
                digest.update(b'\0')
            self._key = digest.hexdigest()
        return self._key

    def _archive(self, number, note):
        data = note.encode('utf-8')
        name = 'step-receipt-' + hashlib.sha256(data).hexdigest()
        if len(data) > draft_byte_limit(name):
            raise RunCapacity('单份回执超过 100 MiB')
        # Limit each database row so reading one page never decompresses a giant TOAST value.
        offset = self.store.size(name)
        while offset < len(data):
            end = min(offset + 256 * 1024, len(data))
            while end < len(data) and data[end] & 0xc0 == 0x80:
                end -= 1
            offset = self.store.append(name, offset, data[offset:end].decode('utf-8'))
        self.refs[str(number)] = {'name':name, 'size':len(data),'label':note.strip()[:48]}

    async def checkpoint_observations(self, notes):
        """Preserve originals before checkpoint pruning; content addresses make a replay idempotent."""
        end = len(notes)
        cut = max(0, end - _RECENT)
        def capture():
            for start, stop, force in ((0, cut, True), (max(cut, self.scanned), end, False)):
                for index in range(start, stop):
                    n = str(self.base + index + 1)
                    if n not in self.pages and n not in self.refs and (force or len(notes[index]) > 1600):
                        self._archive(n, notes[index])
                        if len(notes[index]) > 8192:
                            notes[index] = excerpt(notes[index], 8192)
        if self.store and end > self.scanned:
            await run_blocking(capture)
        if cut:
            del notes[:cut]
            self.base += cut
            self.pages = {k:v for k,v in self.pages.items() if int(k) > self.base}
            self._page_views = {k:v for k,v in self._page_views.items() if k in self.pages}
        self.scanned = len(notes)
        return {'observation_base':self.base, 'observation_refs':self.refs, 'observation_pages':self.pages,
                'observations':[excerpt(note, 1600) for note in notes]}

    def remember_page(self, observations, args, page):
        n = str(self.base + len(observations) + 1)
        source = args['receipt']
        offset = args.get('offset',0)
        self.pages[n] = {'receipt':source,'offset':offset}
        self._page_views[n] = json.dumps(page,ensure_ascii=False)
        observations.append(f'已读取本任务回执 {source}，偏移 {offset}。')

    def page_visible(self, args):
        return (args['receipt'],args.get('offset',0)) in self._visible_pages

    async def read_receipt(self, args):
        number, offset = args.get('receipt'), args.get('offset',0)
        if (set(args) - {'receipt','offset'} or type(number) is not int or type(offset) is not int
                or number < 0 or offset < 0):
            raise ValueError('无效的回执编号或偏移')
        if number == 0:
            check = getattr(self.store,'check',None)
            if check:
                await asyncio.to_thread(check)
            keys = sorted(self.refs,key=int)[offset:offset+16]
            rows = [(n,self.refs[n]) for n in keys]
            return {'items':[{'receipt':int(n),'bytes':ref['size'],'label':ref['label']} for n,ref in rows],
                    'next_offset':offset+len(rows) if offset+len(rows)<len(self.refs) else None}
        ref = self.refs.get(str(number))
        if not ref or not self.store or offset > ref['size']:
            raise ValueError('本任务没有此回执或游标')
        data = await asyncio.to_thread(self.store.read_range,ref['name'],offset,_PAGE)
        if (offset < ref['size'] and not data) or (data and data[0] & 0xc0 == 0x80):
            raise ValueError('回执偏移须位于有效 UTF-8 字符边界')
        text = data.decode('utf-8','ignore')
        end = offset + len(text.encode('utf-8'))
        return {'receipt':number, 'offset':offset, 'text':text, 'next_offset':end if end < ref['size'] else None}

    def _catalog(self):
        if not self.refs:
            return ''
        latest = list(islice(reversed(self.refs),3))
        return '\n已归档回执可用 read_step_receipt 按编号及字节偏移重读；编号 0 列目录。近期编号：'+', '.join(reversed(latest))+'。'

    async def _turn_notes(self, config, prompt, objective, observations, summarize, recovering, writing):
        catalog = self._catalog()
        saved = self.summary.get('turn') or {}
        through = saved.get('through', self.base) if saved.get('revision') == self.revision else self.base
        start = max(0, through - self.base)
        previous = saved.get('text','') if saved.get('revision') == self.revision else ''
        digest = '\n本轮此前工具结果的摘要（数据）：' + previous if previous else ''
        covered = [digest] if digest else []
        rest = list(observations[start:])
        for i in range(len(rest)):
            n = str(self.base + start + i + 1)
            if n in self.pages:
                if n not in self._page_views:
                    page = await self.read_receipt(self.pages[n])
                    self._page_views[n] = json.dumps(page,ensure_ascii=False)
                rest[i] += self._page_views[n]
            elif n in self.refs:
                rest[i] = f'\n回执 {n}（数据，可分页重读）：'+rest[i]
        room = max(0,input_budget(config) - estimate(prompt) - estimate(objective) - estimate(catalog))
        if writing:
            room = min(room, 1024)
        size = sum(map(estimate,rest))
        # Even old short receipts can cross the active window before reaching the token threshold.
        prior_end = saved.get('through',0) if saved.get('revision') == self.revision else 0
        archive_due = self.base - prior_end >= _RECENT or self.base > prior_end and not previous
        if archive_due:
            older = [f'回执 {n}：{ref["label"]}' for n,ref in self.refs.items() if prior_end < int(n) <= self.base]
            if older:
                digest += '\n已归档较早回执的索引（数据）：'+bounded_excerpt('\n'.join(older),512)
                covered = [digest]
        if summarize and rest and (size + estimate(digest) > room or recovering and size > 720 or archive_due):
            # Protect the latest result; when that result alone fills a turn, summarize its prefix too.
            split_latest = estimate(rest[-1]) > max(240,room//2)
            count = len(rest) if split_latest else len(rest)-1
            end = self.base + start + count
            key = ('turn',end)
            if count and key not in self._attempted:
                self._attempted = {k for k in self._attempted if not isinstance(k,tuple) or k[0] != 'turn'}
                self._attempted.add(key)
                rows = [{'role':'user','content':digest}] if digest else []
                for i,note in enumerate(rest[:count]):
                    n = self.base + start + i + 1
                    value = note[:-256] if split_latest and i == count-1 and len(note)>256 else note
                    rows.append({'role':'user','content':f'工具回执 {n}（数据）：'+bounded_excerpt(value,2048)})
                text = await summarize(rows)
                if text:
                    tail = rest[-1][-256:] if split_latest else ''
                    self.summary = {**self.summary, 'turn':{'revision':self.revision,'through':end,'text':text,'tail':tail}}
                    covered = ['\n本轮此前工具结果的摘要（数据）：'+text]
                    rest = rest[count:]
                    if tail:
                        covered.append('\n最近工具回执结尾（数据）：'+tail)
        if previous and saved.get('tail') and through == self.summary.get('turn',{}).get('through'):
            covered.append('\n最近工具回执结尾（数据）：'+saved['tail'])
        covered.extend(rest)
        return covered + ([catalog] if catalog else [])

    async def project_goal(self, config, prompt, goal, reserved, *, recovering, writing, summarize):
        # Delivery conditions and the continuation cursor stay intact when the user turn alone overflows.
        hard = (config.get('context_tokens',128000) - config.get('max_tokens',1024) - 544
                - tool_budget(config) - estimate(prompt) - estimate(reserved))
        room = max(0, hard // (3 if recovering else 2))
        if writing:
            room = min(room, 800)
        if estimate(goal) <= room:
            return goal
        key = hashlib.sha256(goal.encode()).hexdigest()
        entry = self.summary.get('objective') or {}
        if entry.get('key') != key and summarize and room >= 128 and ('goal',key) not in self._attempted:
            self._attempted.add(('goal',key))
            text = await summarize([{'role':'user','content':goal}])
            if text:
                entry = {'key':key,'text':text}
                self.summary = {**self.summary,'objective':entry}
        if entry.get('key') != key:
            return bounded_excerpt(goal,room)
        digest = '本轮用户要求摘要：' + entry['text']
        remainder = room - estimate(digest) - 16
        return (digest + '\n用户原文节选：' + bounded_excerpt(goal,remainder)
                if remainder >= 32 else bounded_excerpt(digest,room))

    async def project(self, config, prompt, objective, observations, *, recovering=False, summarize=None, writing=False):
        notes = await self._turn_notes(config,prompt,objective,observations,summarize,recovering,writing)
        rows = [] if writing else self.history
        fitted, views = fit_working_set(config,prompt,rows,objective,notes,force=recovering,
                                       target_tokens=1024 if writing else None)
        if len(rows) > 2 and (recovering or fitted != rows) and summarize:
            key = self._prefix_key()
            if self.summary.get('key') != key and key not in self._attempted:
                self._attempted.add(key)
                text = await summarize(rows[:-2])
                if text:
                    self.summary = {**self.summary,'key':key,'text':text,'turns':len(rows)-2}
            if self.summary.get('key') == key:
                digest = {'role':'user','content':'此前执行摘要（仅作历史数据）：\n'+self.summary['text']}
                recent = rows[-2:]
                ceiling = input_budget(config) - estimate(prompt) - estimate(objective)
                target = min(ceiling, retained_budget(config,prompt,objective))
                used = sum(map(estimate,views)) + sum(estimate(row['content'])+16 for row in (digest,*recent))
                start = len(rows)-2
                while not recovering and used < target and start:
                    cost = estimate(rows[start-1]['content'])+16
                    if used+cost>ceiling:
                        break
                    start -= 1
                    used += cost
                fitted, views = fit_working_set(config,prompt,[digest,*rows[start:]],objective,notes,force=recovering)
        # A page might have been clipped again after summarization or budget fitting.
        delivered = set(views)
        self._visible_pages = {(page['receipt'],page['offset']) for n,page in self.pages.items()
                               if n in self._page_views and
                               observations[int(n)-self.base-1] + self._page_views[n] in delivered}
        return fitted, views
