"""Run the native model, then execute and observe all calls in each model turn."""
import json
import logging
from dataclasses import replace
from uuid import uuid4

from fastapi import HTTPException

from avatar.conversation.loop.store import BATCH_PREFIX, RunCapacity
from avatar.conversation.loop.turns import stream_final
from avatar.conversation.loop.types import ToolOutcome
from avatar.conversation.tools.activity import without_false_tool_footer
from avatar.core.execution import AgentNeedsInput, check_execution_async, current_tool_call, model_failure, run_blocking
from avatar.models.client import ModelResult, accumulate_usage
from avatar.models.effort import visible_output_config
from avatar.models.limits import check_context, history_row_tokens
from avatar.models.context_budget import input_budget


log = logging.getLogger(__name__)
_CONTINUE = '上一步因单次输出上限截断。请紧接已完成正文续写，不重复前文。若有未完成的工具调用，请根据回执继续。'


class ReActEngine:
    def __init__(self, model, config, key, prompt, context, registry, gate, drafts, journal, policy,
                 delivery, trace, emit, discard, progress, *, summarize=None):
        self.model, self.config, self.key, self.prompt = model, config, key, prompt
        self.context, self.registry, self.gate, self.drafts = context, registry, gate, drafts
        self.journal, self.policy, self.delivery, self.trace = journal, policy, delivery, trace
        self.emit, self.discard, self.progress, self.summarize = emit, discard, progress, summarize
        self.state = journal.data
        self.trajectory = list(self.state.get('trajectory', ()))
        self.usage = self.state.get('usage')
        self.step = self.state.get('step', 0)
        self.seen = dict(self.state.get('seen_calls', {}))
        self.media_calls = set(self.state.get('media_calls', ()))
        for identity in self.media_calls:
            self.seen.pop(identity, None)
        self.prefix = self.state.get('text_prefix', '')
        self.gen_media = {}
        self.preview_prefix = False

    def fields(self, **fields):
        return {'step':self.step, 'trajectory':self.trajectory, 'usage':self.usage,
                'seen_calls':self.seen, 'media_calls':sorted(self.media_calls), 'text_prefix':self.prefix,
                'history_summary':self.context.summary, 'phase':'running',
                'receipt_refs':self.context.receipt_refs,
                'material':self.delivery.prepared if self.delivery else [],
                'material_visible':sorted(self.delivery.visible_ids) if self.delivery else [],
                **fields}

    def account(self, usage):
        self.usage = accumulate_usage(self.usage, usage)

    async def pause(self, reason, *, retry=False, question=None, **fields):
        await self.journal.save(**self.fields(phase='retry' if retry else 'paused', user_question=question, **fields))
        error = AgentNeedsInput.from_context(reason, self.config, self.usage, user_question=question)
        error.stream_retryable = retry
        raise error

    async def final(self, result):
        await check_execution_async(self.registry.validate)
        answer = replace(result, usage=self.usage,
                         material_delivery=tuple(self.delivery.prepared) if self.delivery and self.delivery.prepared else None)
        await self.journal.answer(answer, **self.fields(pending_batch=None, text_prefix='', user_question=None))
        return answer

    async def observation(self, call, outcome):
        value = outcome.value
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        self.trace.tool_result(call['name'], call['arguments'], value)
        text = text.replace(chr(0), '')
        content, ref = await self.context.archive(text)
        return {'role':'tool','tool_call_id':call['id'],'name':call['name'],'content':content,
                'is_error':outcome.is_error, **({'receipt_ref':ref} if ref else {})}

    async def save_batch(self, batch):
        data = json.dumps(batch, ensure_ascii=True, separators=(',', ':')).encode()
        if len(data)<=12*1024:
            await self.journal.save(**self.fields(pending_batch=batch))
            return batch
        name = BATCH_PREFIX + uuid4().hex
        ref = {'ref':name}
        fields = self.fields(pending_batch=ref)
        await run_blocking(self.journal.store.archive, name, data, patch=fields)
        self.state.update(fields)
        return ref

    def emit_candidate(self, text):
        if not self.delivery or not self.delivery.prepared:
            self.emit(text)

    async def complete_batch(self, batch, *, pending=None, capture=False, replay=False):
        frame, receipts = batch['assistant'], dict(batch.get('receipts', {}))
        calls = frame['tool_calls']
        controls = [call for call in calls if (spec := self.registry.tools.get(call['name'])) and spec.control]
        if pending and 'ref' in pending:
            receipts.update(pending.get('receipts', {}))
        action, duplicates = None, 0
        order = [call for call in calls if call not in controls] + controls
        for call in order:
            identifier = call['id']
            if identifier in receipts:
                continue
            if capture or len(controls)>1:
                await check_execution_async(self.registry.validate)
            if len(controls)>1:
                outcome = ToolOutcome.error('同一批含多个相互冲突的控制动作。请在下一轮只选择一个。')
            elif capture:
                try:
                    value = await self.drafts.capture(batch['partials'][identifier]) if call['name'] in self.registry.tools else None
                    outcome = ToolOutcome(value) if value else ToolOutcome.error('工具参数因输出上限截断，请分步重新提交。')
                except RunCapacity:
                    raise
                except (ValueError, HTTPException) as error:
                    if isinstance(error, HTTPException) and error.status_code not in (413,422):
                        raise
                    outcome = ToolOutcome.error(str(getattr(error, 'detail', error)))
            else:
                call_scope = current_tool_call.set((self.step, identifier, replay))
                try:
                    outcome, identity = await self.registry.dispatch(call, self.seen, self.step)
                finally:
                    current_tool_call.reset(call_scope)
                if identity and identity in self.seen:
                    duplicates += 1
                elif identity and not outcome.is_error:
                    self.seen[identity] = self.step
                if media := self.config.pop('_entry_native_media', None):
                    if identity:
                        self.media_calls.add(identity)
                    skipped = False
                    for kind, limit in (('images', 5), ('audio', 1)):
                        values = self.gen_media.setdefault(kind, [])
                        incoming = media.get(kind, [])
                        room = max(0, limit-len(values))
                        values.extend(incoming[:room])
                        skipped |= len(incoming)>room
                    if skipped:
                        outcome = ToolOutcome({'result':outcome.value,
                                               'notice':'本批媒体超过可附加数量。请将该原件分开调用。'}, True)
                        if identity:
                            self.seen.pop(identity, None)
            action = outcome.action or action
            receipts[identifier] = await self.observation(call, outcome)
            if pending is not None and not outcome.action:
                pending = {**pending, 'receipts':receipts}
                await self.journal.save(**self.fields(pending_batch=pending))
        rows = [receipts[c['id']] for c in calls]
        if capture:
            observation = [{'name':r['name'],'content':r['content']} for r in rows]
            self.trajectory.append({'role':'user','content':'截断工具的恢复回执（数据）：'+
                                    json.dumps(observation, ensure_ascii=False)})
        else:
            self.trajectory.extend([frame, *rows])
        return action, bool(rows) and all(row['is_error'] for row in rows), duplicates

    async def request(self, settings, *, recovering=False):
        prompt = self.prompt
        await check_execution_async(self.registry.validate)
        history = await self.context.project(settings, prompt, self.trajectory, recovering=recovering,
                                             summarize=self.summarize)
        del self.trajectory[:self.context.omitted_count]
        settings = {**settings, '_image_inputs':self.config.get('_image_inputs', ()),
                    '_audio_inputs':self.config.get('_audio_inputs', ())}
        if self.gen_media:
            history.append({'role':'user','content':'此处为上一批工具读取的授权原件媒体。请结合对应文件回执使用。'})
            settings.update(_image_inputs=self.gen_media.get('images', ()), _audio_inputs=self.gen_media.get('audio', ()))
        headroom = check_context(settings, prompt, history, None)
        self.config['_session_page_bytes'] = max(4, (headroom*3-512)//2)
        await check_execution_async(self.registry.validate)
        status = {'type':'status','stage':'thinking','message':'正在根据本轮输入和已有回执生成结果'}
        if self.progress:
            self.progress(status)

        def report(event):
            if self.progress:
                self.progress(event)
            if event.get('reset_reply'):
                self.preview_prefix = False
                self.show_prefix()

        self.show_prefix()
        with self.trace.step('reason', {'iteration':self.step,'tools':[t['name'] for t in settings['_tools']]}) as output:
            source = self.model.stream(settings, self.key, prompt, history, None)
            result = await stream_final(source, self.emit_candidate, self.registry.validate, report)
            output.update(finish_reason=result.finish_reason, usage=result.usage,
                          tools=[call['name'] for call in result.tool_calls])
        self.gen_media = {}
        return result

    def show_prefix(self):
        if self.prefix and not self.preview_prefix:
            self.emit_candidate(self.prefix)
            self.preview_prefix = True

    def reset(self):
        self.discard()
        self.preview_prefix = False

    async def control(self, action, pending):
        name, args = action['name'], action['arguments']
        if name=='ask_user':
            try:
                await self.pause('等待你补充任务信息', question=args, pending_batch=None)
            except AgentNeedsInput:
                await self.release_batch(pending)
                raise
        if name=='require_conversation_tool':
            if self.progress:
                self.progress({'type':'notice','tone':'required','message':args['content']})
            calls = ()
            if self.policy.require_terminal_tool:
                calls = ({'name':'wait_for_user','arguments':{'content':args['content'],'reason':args['content'],
                          'who':self.config['_conversation_tool_owner']}},)
            return await self.final(ModelResult(args['content'], None, calls, 'tool_required'))
        if name=='contact_avatar':
            from avatar.conversation.collaboration.intent import CONTACT_REPLY
            return await self.final(ModelResult(CONTACT_REPLY, None, finish_reason='stop', contact_draft=args))
        return await self.final(ModelResult(args['content'], None, (action,), 'tool_calls'))

    async def clear_batch(self, pending):
        await self.journal.save(**self.fields(pending_batch=None))
        await self.release_batch(pending)

    async def release_batch(self, pending):
        if pending and 'ref' in pending:
            await run_blocking(self.journal.store.discard, pending['ref'])

    async def run(self):
        errors = repeats = continuations = corrections = 0
        recovering = False
        settings = {**self.config, '_tools':self.registry.schemas(), '_batch_tools':True,
                    '_optional_tools':True, '_react_tool_calls':True, '_retain_turn_fragments':True,
                    '_call_purpose':'react'}
        if pending := self.state.get('pending_batch'):
            batch = json.loads(await run_blocking(self.journal.store.read, pending['ref'])) if 'ref' in pending else pending
            action, _, _ = await self.complete_batch(batch, pending=pending, capture=bool(batch.get('partials')), replay=True)
            if action:
                answer = await self.control(action, pending)
                await self.release_batch(pending)
                return answer
            await self.clear_batch(pending)
        for remaining in range(self.policy.max_steps-1, -1, -1):
            self.step += 1
            if remaining == 1:
                self.trajectory.append({'role':'user','content':'剩余步骤有限。请汇总已核验结果或提交必要的结束动作。'})
            force_finish = remaining==0 or errors>=self.policy.max_consecutive_errors or repeats>=self.policy.max_duplicate_calls
            if force_finish and (self.policy.require_terminal_tool or self.gate.html_missing or self.drafts.active):
                await self.pause('任务尚缺必要回执，进度已保存，可重试当前步骤。', retry=True)
            current = {**settings, '_tools':[]} if force_finish else settings
            try:
                result = await self.request(current, recovering=recovering)
            except HTTPException as error:
                self.reset()
                self.account(getattr(error,'model_usage',None))
                if getattr(error,'stream_exhausted',False):
                    await self.pause(str(error.detail), retry=True)
                if getattr(error,'stop_reason',None)=='context_overflow':
                    if recovering:
                        await self.pause('压缩后必要输入仍超过模型窗口。已保留回执，可调整输入后继续。', retry=True)
                    recovering = True
                    continue
                if getattr(error,'invalid_tool_call',False) and not force_finish:
                    errors += 1
                    self.trajectory.append({'role':'user','content':'上一步工具调用无效。请按工具参数定义重新提交。'})
                    continue
                log.warning('ReAct model failed run=%s step=%s status=%s reason=%s',
                            getattr(self.journal.store,'run_id',None),self.step,error.status_code,model_failure(error)['reason'])
                raise
            self.account(result.usage)
            recovering = False
            if result.stop_reason=='output_limit':
                continuations += 1
                self.prefix += result.text
                self.preview_prefix = bool(self.prefix)
                if not result.text:
                    settings = visible_output_config(settings)
                declared = {schema['name'] for schema in current['_tools']}
                calls = [call for call in (*result.tool_calls, *result.partial_tool_calls) if call['name'] in declared]
                pending = None
                if calls:
                    self.reset()
                    minimal = [{k:call[k] for k in ('id','name')} | {'arguments':{}} for call in calls]
                    batch = {'assistant':{'role':'assistant','content':'','tool_calls':minimal},
                             'partials':{call['id']:call for call in calls}}
                    pending = await self.save_batch(batch)
                    await self.complete_batch(batch, pending=pending, capture=True)
                if result.text:
                    self.trajectory.append({'role':'assistant','content':result.text})
                self.trajectory.append({'role':'user','content':_CONTINUE})
                await self.clear_batch(pending)
                if continuations>=self.policy.max_continuations or remaining==0:
                    await self.pause('本轮输出多次达到模型上限，已保留可接续的内容。', retry=True)
                continue
            if result.tool_calls:
                self.reset()
                self.prefix = ''
                if force_finish:
                    await self.pause('模型未能完成收尾，已保留执行进度。', retry=True)
                frame = {'role':'assistant','content':result.text,'tool_calls':list(result.tool_calls)}
                if result.replay:
                    frame['replay'] = result.replay
                durable = any((spec := self.registry.tools.get(call['name'])) and spec.checkpoint for call in result.tool_calls)
                batch = {'assistant':frame}
                pending = await self.save_batch(batch) if durable else None
                action, failed, duplicate = await self.complete_batch(batch, pending=pending)
                if history_row_tokens(frame) > input_budget(current):
                    await self.context.project(current, self.prompt, self.trajectory)
                    del self.trajectory[:self.context.omitted_count]
                if action:
                    answer = await self.control(action, pending)
                    await self.release_batch(pending)
                    return answer
                errors = errors + 1 if failed else 0
                repeats = repeats + duplicate if failed else 0
                await self.clear_batch(pending)
                continue
            text = self.prefix + result.text
            if self.config.get('_sandbox_succeeded'):
                filtered = without_false_tool_footer(text)
                if filtered!=text:
                    self.reset()
                text = filtered
            if issue := self.gate.check(text):
                self.reset()
                if corrections>=self.policy.finish_corrections or remaining==0:
                    await self.pause(issue + ' 进度已保存，可重试。', retry=True)
                corrections += 1
                self.trajectory.extend([{'role':'assistant','content':result.text}, {'role':'user','content':issue}])
                self.prefix = ''
                continue
            return await self.final(replace(result,text=text))
        await self.pause('已达到本轮执行步数上限，进度已保存，可重试。', retry=True)
