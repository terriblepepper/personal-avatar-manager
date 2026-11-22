"""Obtain and validate the model's plan before any domain action."""
import json

from fastapi import HTTPException

from avatar.conversation.working_context import fit_turn
from avatar.core.execution import call_with_retry, check_execution_async
from avatar.models.client import accumulate_usage
from avatar.models.diagnostics import stop_reason
from avatar.models.limits import check_context


async def plan_checks(model, settings, key, system, history, question, choices, validate, *, revise=False):
    from avatar.conversation.tools.material_delivery import FindMaterial
    plan_tool = {
        'name': 'plan_task',
        'description': '记录当前请求中仍需工具处理的目标，每项对应一个可核对的结果。',
        'parameters': {'type': 'object', 'properties': {'steps': {'type': 'array', 'maxItems': 8, 'items': {
            'type': 'object', 'properties': {'goal': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                                             'tool': {'type': 'string', 'enum': sorted(choices)},
                                             'lookup': FindMaterial.model_json_schema(), 'html_mode': {'type':'string','enum':['2d','3d']}},
            'required': ['goal', 'tool'], 'additionalProperties': False}}},
            'required': ['steps'], 'additionalProperties': False},
    }
    if revise:
        plan_tool['parameters']['properties']['continue_draft'] = {'type':'boolean',
            'description':'原草稿仍符合全部补充要求，且下一步应从其游标继续时选择 true。'}
    config = {**settings, '_tools': [plan_tool], '_batch_tools':True, '_optional_tools': True, '_call_purpose': 'task_planning'}
    prompt = (system + '\n先分析当前用户需要的最终结果，以及已有资料和回执是否足够。'
              '列出真正缺少的工具步骤，保留主题、范围和交付条件。解释概念、介绍协议、写作，'
              '或已有证据足以回答时，返回空 steps。仅提及工具、计算或文件不代表需要执行。'
              'deliver_material 将资料库原件交付给本人。联络他人用 contact_avatar，收件人与原件清楚时直接准备草稿；'
              '需确认当前会话原件时先 find_conversation_files，资料库原件可先 find_material。'
              '确实需要一个已关闭工具时，仍列出它的 ID。以下是本步可选工具及其用途：'
              + json.dumps(choices, ensure_ascii=False) +
              '。资料检索步骤可附 lookup，按对应工具填写 query、folder、mode 或 offset。每项只处理一个目标，后项依赖前项结果时省略 lookup。两种动画各列一个 create_animation 步骤，并写明 html_mode。'
              '只提交 plan_task 或 JSON 对象。steps 是最多八项的数组，每项含 goal 和 tool；无需工具时用 {"steps":[]}。不执行工具，不输出正文或思考。')
    totals = None
    draft = ''
    overflow = False
    if revise:
        prompt += '只列剩余目标。根据本轮补充和当前草稿，填写 continue_draft；需要改写前文或换作品时选 false。'
    for attempt in range(2):
        await check_execution_async(validate)
        plan_prompt = prompt if not attempt else prompt + '\n上次未取得有效计划。请直接提交符合结构的计划。'
        options = config if not attempt else {**config, '_tools': []}
        rows, message = fit_turn(options, plan_prompt, history[-2:] if overflow else history, question, [draft] if draft else [])
        try:
            check_context(options, plan_prompt, rows, message)
        except HTTPException:
            overflow = True
            continue
        try:
            result = await call_with_retry(lambda: model.generate(options, key, plan_prompt, rows, message),
                                           validate, retry_invalid=False)
        except HTTPException as error:
            if getattr(error, 'stop_reason', None) == 'context_overflow':
                totals = accumulate_usage(totals, getattr(error, 'model_usage', None))
                overflow = True
                continue
            if not (getattr(error, 'invalid_tool_call', False) or getattr(error, 'empty_model_output', False)
                    or getattr(error, 'output_limited', False)):
                raise
            totals = accumulate_usage(totals, getattr(error, 'model_usage', None))
            if getattr(error, 'recovery_attempted', False):
                break
            continue
        totals = accumulate_usage(totals, result.usage)
        try:
            if stop_reason(result.finish_reason) == 'output_limit':
                draft = '\n上次计划被截断，原动作均未执行。这次每项目标只用一个短句。'
                continue
            if not result.tool_calls:
                plan = json.loads(result.text.strip().removeprefix('```json').removesuffix('```'))
            elif revise and len(result.tool_calls)==1 and result.tool_calls[0]['name']=='plan_task':
                plan = result.tool_calls[0]['arguments']
            elif all(call['name']=='plan_task' and isinstance(call['arguments'],dict)
                     and set(call['arguments'])=={'steps'} and isinstance(call['arguments']['steps'],list)
                     for call in result.tool_calls):
                plan = {'steps':[step for call in result.tool_calls for step in call['arguments']['steps']]}
            else:
                continue
            keep_draft = plan.pop('continue_draft', False) if revise and isinstance(plan,dict) else False
            if type(keep_draft) is not bool or not isinstance(plan, dict) or set(plan) != {'steps'}:
                continue
            steps = plan['steps']
            if (isinstance(steps, list) and len(steps) <= 8 and all(
                    isinstance(step, dict) and {'goal','tool'} <= set(step) <= {'goal','tool','lookup','html_mode'} and
                    isinstance(step['goal'], str) and 0 < len(step['goal'].strip()) <= 160 and
                    isinstance(step['tool'], str) and step['tool'] in choices for step in steps)):
                for step in steps:
                    if 'html_mode' in step and (step['tool'] != 'create_animation' or step['html_mode'] not in ('2d','3d')):
                        raise ValueError()
                    if 'lookup' in step:
                        if step['tool'] not in ('find_material','find_animation','find_public_material'):
                            raise ValueError()
                        step['lookup'] = FindMaterial.model_validate(step['lookup']).model_dump(exclude_unset=True)
                return steps, totals, keep_draft
        except (ValueError, TypeError):
            continue
    return None, totals, False
