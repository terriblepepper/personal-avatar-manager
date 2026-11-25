"""Project the model's working context and submit a complete decision request."""
from dataclasses import dataclass
import json

from fastapi import HTTPException

from avatar.conversation.loop.file_context import explicit_file_context
from avatar.conversation.loop.planning import needs_confirmation
from avatar.conversation.loop.runtime import verified_context
from avatar.core.execution import call_with_retry, recoverable
from avatar.core.tokens import estimate
from avatar.models.context_budget import bounded_excerpt, compression_budget, input_budget


@dataclass(frozen=True)
class TurnInput:
    settings: dict
    prompt: str
    history: list
    message: str
    notes: list
    compressed: bool

    async def generate(self, model, key, validate, account):
        async def request():
            try:
                result = await model.generate(self.settings, key, self.prompt, self.history, self.message)
            except HTTPException as error:
                if (recoverable(error) and not getattr(error, 'request_not_sent', False)) or getattr(error, 'model_usage', None) is not None:
                    account(getattr(error, 'model_usage', None))
                raise
            account(result.usage)
            return result
        return await call_with_retry(request, validate, retry_invalid=False)


async def project_turn(context, step_settings, step_prompt, task_goal, pending, receipts_log, checked_files,
                       *, observations, context_retry, draft, writing, summarizer):
    continuing = draft.active
    goal_view = ([{k:v for k,v in step.items() if k in ('goal_no','tool','file_access') or
                   (k=='goal' and i==0)} for i,step in enumerate(pending)] if context_retry else pending)
    checklist = '\n待核验目标（本轮计划，仅作数据）：'+json.dumps(goal_view,ensure_ascii=False) if goal_view else ''
    current_receipts = [receipt for receipt in receipts_log if receipt.get('input_id') == context.revision]
    if any(needs_confirmation(s) for s in pending):
        goal_numbers = {step['goal_no'] for step in pending}
        evidence = [{'id':r.get('id',i),'tool':r['tool'],'goal_no':r.get('goal_no'),
                     **{k:r[k] for k in ('mode','result') if r.get(k)}} for i,r in enumerate(receipts_log,1)
                    if r.get('input_id') == context.revision and r.get('goal_no') in goal_numbers]
        checklist += '\n最近可引用的回执：'+json.dumps(evidence,ensure_ascii=False)
        checklist += '\nupdate_task 只使用上述编号；目标尚无有效回执时先调用其工具。历史回执内容可参考，编号不能用于新计划。'
    checklist += verified_context(current_receipts, checked_files, compact=context_retry)
    continuation = ''
    if continuing:
        available = max(0, compression_budget(step_settings)-estimate(step_prompt))
        occupied = (estimate(task_goal) + estimate(checklist) + sum(map(estimate,observations))
                    + sum(estimate(row['content'])+16 for row in context.history))
        # During compaction the current draft and the remaining task each need room to continue.
        view_tokens = max(available//2,available-occupied)
        if context_retry:
            view_tokens //= 2
        continuation = '\n' + await draft.request(compact=context_retry,input_tokens=view_tokens)
    overhead = estimate(step_prompt)+estimate(continuation)
    room = max(0, compression_budget(step_settings)-overhead)
    if estimate(checklist)+estimate(task_goal)>room:
        if not room:
            room = max(0,input_budget(step_settings)-overhead)
        checklist = bounded_excerpt(checklist,max(room//2,room-estimate(task_goal)))
    previous_summary = context.summary
    originals = explicit_file_context(step_settings,step_prompt,task_goal+checklist+continuation)
    if not originals and step_settings.get('_explicit_session_files'):
        originals = explicit_file_context(step_settings,step_prompt,checklist+continuation,limit=room//2)
    scope = await context.project_goal(step_settings, step_prompt, task_goal,
                                            checklist + continuation + originals, recovering=context_retry,
                                            writing=writing, summarize=summarizer)
    objective = scope + checklist + originals + continuation
    working_history, notes = await context.project(
        step_settings, step_prompt, objective, observations, recovering=context_retry,
        summarize=summarizer, writing=writing)
    message = scope + checklist + originals + ''.join(notes) + continuation
    compressed = (working_history != context.history or scope != task_goal
                  or context.summary is not previous_summary
                  or sum(map(len, notes)) < sum(map(len, observations)))
    return TurnInput(step_settings, step_prompt, working_history, message, notes, compressed)
