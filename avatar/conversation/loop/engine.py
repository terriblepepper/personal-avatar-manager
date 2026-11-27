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
from avatar.conversation.collaboration.intent import CONTACT_REPLY


log = logging.getLogger(__name__)
_MEDIA_LIMITS = {'images':5, 'audio':1}
_CONTINUE = '上一步因单次输出上限截断。请紧接已完成正文续写，不重复前文。若有未完成的工具调用，请根据回执继续。'


class ReActEngine:
    def __init__(self, model, config, key, prompt, context, registry, gate, drafts, journal, policy,
                 delivery, trace, emit, discard, progress, *, unlocked, summarize=None):
        self.model, self.config, self.key, self.prompt = model, config, key, prompt
        self.context, self.registry, self.gate, self.drafts = context, registry, gate, drafts
        self.journal, self.policy, self.delivery, self.trace = journal, policy, delivery, trace
        self.emit, self.discard, self.progress, self.summarize = emit, discard, progress, summarize
        self.state = journal.data
        self.unlocked = unlocked
        self.trajectory = list(self.state.get('trajectory', ()))
        self.usage = self.state.get('usage')
        self.step = self.state.get('step', 0)
        self.seen = dict(self.state.get('seen_calls', {}))
        for identity in self.state.get('media_calls', ()):
            self.seen.pop(identity, None)
        self.prefix = self.state.get('text_prefix', '')
        self.gen_media = {}
        self.preview_prefix = False

    def fields(self, **fields):
        return {'step':self.step, 'trajectory':self.trajectory, 'usage':self.usage,
                'seen_calls':self.seen, 'media_calls':[], 'text_prefix':self.prefix,
                'history_summary':self.context.summary, 'phase':'running',
                'receipt_refs':self.context.receipt_refs, 'unlocked_tools':list(self.unlocked),
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

    def invalidate_seen(self, prefix):
        self.seen = {key:step for key,step in self.seen.items() if not key.startswith(prefix + ':')}

    def consume_projection(self):
        if omitted := self.context.omitted_count:
            del self.trajectory[:omitted]
            self.invalidate_seen('read_step_receipt')

    def media(self, kind):
        values = []
        setting = '_image_inputs' if kind=='images' else '_audio_inputs'
        for item in (*self.config.get(setting, ()),
                     *self.gen_media.get(kind, ())):
            if item not in values:
                values.append(item)
        return values

    def unlock(self, names):
        added = []
        for name in names:
            if self.registry.activatable(name) and name not in self.unlocked:
                self.unlocked.append(name)
                added.append(name)
        return added

    async def complete_batch(self, batch, *, pending=None, capture=False, replay=False):
        frame, receipts = batch['assistant'], dict(batch.get('receipts', {}))
        calls = frame['tool_calls']
        # Older v2 checkpoints were created with every authorized tool declared.
        declared = batch.get('declared', self.registry.tools.keys() - {'enable_tools'})
        active = {name for name in declared if self.registry.available(name)}
        controls = [call for call in calls if call['name'] in active and self.registry.tools[call['name']].control]
        if pending and 'ref' in pending:
            receipts.update(pending.get('receipts', {}))
        action, duplicates, advanced, interrupted = None, 0, False, None
        previous_media = self.gen_media
        if not capture:
            self.gen_media = {}
        order = [call for call in calls if call not in controls] + controls
        for call in order:
            identifier = call['id']
            if identifier in receipts:
                continue
            if capture or len(controls)>1:
                await check_execution_async(self.registry.validate)
            if interrupted:
                outcome = ToolOutcome.error('上一项操作的结果尚未确认，本项未执行。')
            elif len(controls)>1:
                outcome = ToolOutcome.error('同一批含多个相互冲突的控制动作。请在下一轮只选择一个。')
            elif capture and (call['name'] not in active or not self.registry.available(call['name'])):
                outcome = ToolOutcome.error('当前会话未授权此工具。请选择已声明的工具。')
            elif capture:
                try:
                    call_scope = current_tool_call.set((self.step, identifier, replay))
                    try:
                        value = await self.drafts.capture(batch['partials'][identifier])
                    finally:
                        current_tool_call.reset(call_scope)
                    outcome = ToolOutcome(value) if value else ToolOutcome.error('工具参数因输出上限截断，请分步重新提交。')
                    advanced |= bool(value and value['status'] in ('truncated_draft_saved','draft_appended'))
                except RunCapacity:
                    raise
                except (ValueError, HTTPException) as error:
                    if isinstance(error, HTTPException) and error.status_code not in (413,422):
                        raise
                    outcome = ToolOutcome.error(str(getattr(error, 'detail', error)))
            else:
                call_scope = current_tool_call.set((self.step, identifier, replay))
                try:
                    outcome, identity = await self.registry.dispatch(call, self.seen, self.step, active=active)
                except AgentNeedsInput as error:
                    interrupted = error
                    outcome, identity = ToolOutcome.error(str(error)), None
                finally:
                    current_tool_call.reset(call_scope)
                if identity and identity in self.seen:
                    duplicates += 1
                elif identity and not outcome.is_error:
                    self.seen[identity] = self.step
                    if self.registry.tools[call['name']].checkpoint:
                        self.invalidate_seen('session_directory')
                if media := self.config.pop('_entry_native_media', None):
                    if identity:
                        self.seen.pop(identity, None)
                    overflow = False
                    for kind, limit in _MEDIA_LIMITS.items():
                        incoming = media.get(kind, ())
                        if not incoming:
                            continue
                        current = self.media(kind)
                        for item in incoming:
                            if item not in current:
                                if len(current)>=limit:
                                    overflow = True
                                else:
                                    current.append(item)
                                    self.gen_media.setdefault(kind, []).append(item)
                    if overflow:
                        outcome = replace(outcome, value={'result':outcome.value,
                            'notice':'超额工具媒体未附加。请依据实际附加的媒体和回执正文作答。'})
            advanced |= bool(self.unlock(outcome.activated))
            action = outcome.action or action
            receipts[identifier] = await self.observation(call, outcome)
            if pending is not None and not outcome.action and not interrupted:
                pending = {**pending, 'receipts':receipts}
                await self.journal.save(**self.fields(pending_batch=pending))
        rows = [receipts[c['id']] for c in calls]
        failed = all(row['is_error'] for row in rows)
        if not capture and failed:
            self.gen_media = previous_media
        if capture:
            observation = [{'name':r['name'],'content':r['content']} for r in rows]
            self.trajectory.append({'role':'user','content':'截断工具的恢复回执（数据）：'+
                                    json.dumps(observation, ensure_ascii=False)})
        else:
            self.trajectory.extend([frame, *rows])
        if interrupted:
            self.account(interrupted.usage)
            interrupted.usage = self.usage
            await self.journal.save(**self.fields(phase='paused', user_question=interrupted.user_question, pending_batch=None))
            await self.release_batch(pending)
            raise interrupted
        return action, bool(rows) and failed and not advanced, duplicates, advanced

    async def request(self, settings, *, force_finish, recovering=False):
        prompt = self.prompt
        await check_execution_async(self.registry.validate)
        while True:
            self.registry.sync_state(self.unlocked)
            tools = [] if force_finish else self.registry.active_schemas(self.unlocked)
            settings = {**settings, '_tools':tools}
            disclosed = list(self.unlocked)
            history = await self.context.project(settings, prompt, self.trajectory, recovering=recovering,
                                                 summarize=self.summarize)
            self.consume_projection()
            # Projection can create a receipt the upcoming request should be able to read.
            if force_finish or self.registry.sync_state(self.unlocked) == disclosed:
                break
        settings = {**settings, '_image_inputs':self.media('images')[:_MEDIA_LIMITS['images']],
                    '_audio_inputs':self.media('audio')[:_MEDIA_LIMITS['audio']]}
        if self.gen_media:
            history.append({'role':'user','content':'此处为本轮工具读取的授权原件媒体。请结合对应文件回执使用。'})
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
        return result, settings

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
            return await self.final(ModelResult(CONTACT_REPLY, None, finish_reason='stop', contact_draft=args))
        return await self.final(ModelResult(args['content'], None, (action,), 'tool_calls'))

    async def clear_batch(self, pending):
        await self.journal.save(**self.fields(pending_batch=None))
        await self.release_batch(pending)

    async def release_batch(self, pending):
        if pending and 'ref' in pending:
            await run_blocking(self.journal.store.discard, pending['ref'])

    async def run(self):
        errors = repeats = stalled_continuations = corrections = 0
        recovering = prefer_visible = False
        settings = {**self.config, '_batch_tools':True,
                    '_optional_tools':True, '_react_tool_calls':True, '_retain_turn_fragments':True,
                    '_call_purpose':'react'}
        if pending := self.state.get('pending_batch'):
            batch = json.loads(await run_blocking(self.journal.store.read, pending['ref'])) if 'ref' in pending else pending
            replay_meta = 'enable_tools' in batch.get('declared', ()) and 'enable_tools' not in self.registry.tools
            if replay_meta:
                self.registry.register_enable_tools(self.unlocked, replay=True)
            try:
                action, _, _, _ = await self.complete_batch(
                    batch, pending=pending, capture=bool(batch.get('partials')), replay=True)
            finally:
                if replay_meta:
                    del self.registry.tools['enable_tools']
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
            current = visible_output_config(settings) if prefer_visible else settings
            prefer_visible = False
            try:
                result, current = await self.request(current, force_finish=force_finish, recovering=recovering)
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
            declared = [schema['name'] for schema in current['_tools']]
            if result.stop_reason=='output_limit':
                fragments = (*result.tool_calls, *result.partial_tool_calls)
                calls = [call for call in fragments if call['name'] in declared]
                hidden = {call['name']: None for call in fragments if call['name'] not in declared}
                activated = self.unlock(hidden) if not force_finish else []
                hint = ''
                if activated:
                    hint += f"以下工具已加载完整定义：{'、'.join(activated)}。请在下一步按定义调用或继续作答。"
                if hidden.keys() - set(activated):
                    hint += '上一轮有截断工具未声明。请使用已声明的工具或直接回复。'
                pending, advanced = None, False
                if calls:
                    self.reset()
                    self.prefix = ''
                    minimal = [{k:call[k] for k in ('id','name')} | {'arguments':{}} for call in calls]
                    batch = {'assistant':{'role':'assistant','content':'','tool_calls':minimal},
                             'declared':declared, 'partials':{call['id']:call for call in calls}}
                    pending = await self.save_batch(batch)
                    _, _, _, advanced = await self.complete_batch(batch, pending=pending, capture=True)
                else:
                    if result.text:
                        self.prefix += result.text
                        self.preview_prefix = True
                        self.trajectory.append({'role':'assistant','content':result.text})
                        advanced = True
                    prefer_visible = not result.text.strip() and not activated
                    if not fragments or result.text.strip():
                        hint += _CONTINUE
                if hint:
                    self.trajectory.append({'role':'user','content':hint})
                advanced |= bool(activated)
                stalled_continuations = 0 if advanced else stalled_continuations+1
                await self.clear_batch(pending)
                if stalled_continuations>=self.policy.max_continuations or remaining==0:
                    await self.pause('本轮输出截断，已保留可接续的内容。', retry=True)
                continue
            stalled_continuations = 0
            if result.tool_calls:
                self.reset()
                self.prefix = ''
                if force_finish:
                    await self.pause('模型未能完成收尾，已保留执行进度。', retry=True)
                frame = {'role':'assistant','content':result.text,'tool_calls':list(result.tool_calls)}
                if result.replay:
                    frame['replay'] = result.replay
                durable = any(call['name'] in declared and self.registry.tools[call['name']].checkpoint
                              for call in result.tool_calls)
                batch = {'assistant':frame, 'declared':declared}
                pending = await self.save_batch(batch) if durable else None
                action, failed, duplicate, _ = await self.complete_batch(batch, pending=pending)
                if history_row_tokens(frame) > input_budget(current):
                    await self.context.project(current, self.prompt, self.trajectory)
                    self.consume_projection()
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
                if result.text.strip():
                    self.trajectory.append({'role':'assistant','content':result.text})
                self.trajectory.append({'role':'user','content':issue})
                self.prefix = ''
                continue
            return await self.final(replace(result,text=text))
        await self.pause('已达到本轮执行步数上限，进度已保存，可重试。', retry=True)
