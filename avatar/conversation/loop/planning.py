"""Disclose authorized tools before execution and retain independently verifiable goals."""
from dataclasses import dataclass
import json

from fastapi import HTTPException

from avatar.core.text_budget import TEXT_LIMIT, fits_text_budget, budget_error
from avatar.conversation.loop.file_context import explicit_file_context
from avatar.models.context_budget import bounded_excerpt, fit_turn, input_budget
from avatar.core.tokens import estimate
from avatar.core.execution import AskUser, call_with_retry, check_execution_async
from avatar.models.client import accumulate_usage
from avatar.models.diagnostics import stop_reason
from avatar.models.limits import check_context


def available_tools(config):
    from avatar.conversation.collaboration.intent import TOOLS
    from avatar.conversation.tools.html import CREATE_HTML
    from avatar.conversation.tools.material_delivery import DELIVERY_TOOLS
    from avatar.conversation.loop.context import READ_STEP_RECEIPT
    from avatar.conversation.tools.builtin import CURRENT_TIME

    domain = config.get('_tools', ())
    allowed = {tool['name']: tool for tool in domain}
    allowed.update((t['name'],t) for t in config.get('_entry_tools', ()))
    owner = config.get('_material_owner')
    personal = owner and not domain and config.get('_personal_conversation')
    if owner:
        allowed.update((tool['name'], tool) for tool in DELIVERY_TOOLS)
    if config.get('_html_enabled'):
        allowed['create_html'] = CREATE_HTML
    for tool in TOOLS:
        name = tool['name']
        if ((name == 'continue_answer' and (not domain or config.get('_optional_tools')))
                or (name == 'deliver_material' and owner)
                or (personal and name=='contact_avatar')):
            allowed[name] = tool
    disabled = {t['id']: t for t in config.get('_conversation_tools', ()) if t['id'] != 'animation' and not t['enabled']}
    if config.get('_allow_user_questions', True):
        allowed['ask_user'] = {'name':'ask_user', 'description':'需要用户补充时提出一个具体问题。可给最多三个简短建议，用户也可自由回答。',
            'parameters':AskUser.model_json_schema()}
    else:
        allowed.pop('ask_user', None)
    if disabled:
        allowed['require_conversation_tool'] = {'name':'require_conversation_tool','description':'提示用户开启当前确实需要的已关闭工具。',
            'parameters':{'type':'object','properties':{'tool_id':{'type':'string','enum':list(disabled)}},
                          'required':['tool_id'],'additionalProperties':False}}
    allowed['read_step_receipt'] = READ_STEP_RECEIPT
    allowed[CURRENT_TIME['name']] = CURRENT_TIME
    return allowed


def disclosure_catalog(config, allowed):
    catalog = {name:' '.join(tool['description'].split())[:220] for name,tool in allowed.items()}
    catalog.update({t['id']:t['name']+'（已关闭）' for t in config.get('_conversation_tools', ())
                    if t['id'] != 'animation' and not t['enabled']})
    return catalog


def _selected(value, choices):
    if not isinstance(value,list) or len(value)>len(choices) or any(not isinstance(n,str) or n not in choices for n in value):
        raise ValueError('选择了当前目录之外的工具')
    return list(dict.fromkeys(value))


def final_reply_step(step):
    return step.get('tool')=='continue_answer' and all(name=='continue_answer' for name in step.get('tools',()))


def lookup_types(config, allowed):
    from avatar.conversation.tools.material_delivery import FindMaterial
    planned = {name:FindMaterial for name in ('find_material','find_public_material') if name in allowed}
    planned.update((name,schema) for name,schema in config.get('_entry_lookup_types',{}).items() if name in allowed)
    return planned


def validate_disclosure(value, choices, *, lookups=None, revise=False, allow_reply=False):
    lookups = lookups or {}
    fields = {'tools','steps','continue_draft'} if revise else {'tools','steps'}
    if allow_reply:
        fields.add('reply')
    if not isinstance(value,dict) or set(value)-fields or not isinstance(value.get('steps'),list):
        raise ValueError('披露缺少子目标')
    if len(value['steps'])>16:
        raise ValueError('单次披露最多十六项子目标，可后续补充')
    selected = _selected(value.get('tools',[]),choices)
    if reply := value.get('reply'):
        if not isinstance(reply, str) or not reply.strip() or selected or value['steps'] or not fits_text_budget(reply):
            raise ValueError('直接答复须包含完整正文，且不能同时选择工具或目标')
        return {'tools': [], 'steps': [], 'reply': reply}
    steps=[]
    text_parts=[]
    for source in value['steps']:
        if not isinstance(source,dict) or set(source)-{'goal','tool','tools','criterion','lookup','segmented','file_access'}:
            raise ValueError('子目标结构无效')
        s=dict(source)
        if not isinstance(s.get('goal'),str) or not s['goal'].strip():
            raise ValueError('缺少子目标说明')
        if 'criterion' in s and (not isinstance(s['criterion'],str) or not s['criterion'].strip()):
            raise ValueError('验收条件无效')
        tools=_selected(s.get('tools',[]),choices)
        primary=s.get('tool') or (tools[0] if tools else '')
        if primary and (not isinstance(primary,str) or primary not in choices):
            raise ValueError('未授权的子目标工具')
        s['tool']=primary
        if len(set(tools)|({primary} if primary else set()))>1 and {'contact_avatar','end_task','send_message','wait_for_user','send_public_material'} & set([primary,*tools]):
            raise ValueError('需先完成准备步骤，再单独提交终态或通信动作')
        if tools:
            s['tools']=list(dict.fromkeys(([primary] if primary else [])+tools))
        if 'segmented' in s and (type(s['segmented']) is not bool or primary!='create_html'):
            raise ValueError('只有作品子目标支持分段写作')
        if 'file_access' in s and (primary!='read_session_file' or s['file_access'] not in ('directory','content')):
            raise ValueError('会话文件目标须选择查目录或读原件')
        if 'lookup' in s:
            proposed = s.pop('lookup')
            if schema := lookups.get(primary):
                try:
                    s['lookup']=schema.model_validate(proposed).model_dump(exclude_unset=True)
                except ValueError:
                    pass  # The selected tool can ask the model for corrected parameters during execution.
        selected.extend(n for n in [primary,*tools] if n and n not in selected)
        text_parts.extend((s['goal'], s.get('criterion',''), s.get('lookup',{}).get('query') or ''))
        if not final_reply_step(s):
            steps.append(s)
    if not fits_text_budget(*text_parts):
        raise ValueError(budget_error())
    if type(value.get('continue_draft',False)) is not bool:
        raise ValueError('计划规模或草稿决定无效')
    ret={'tools':selected,'steps':steps}
    if revise:
        ret['continue_draft']=value.get('continue_draft',False)
    return ret


async def disclose_tools(model, settings, key, system, history, question, catalog, validate, *, lookups=None, revise=False, allow_reply=False):
    lookup_schemas = list(dict.fromkeys((lookups or {}).values()))
    name={'type':'string','enum':list(catalog)}
    names={'type':'array','items':name,'uniqueItems':True}
    steps={'type':'array','maxItems':16,'items':{'type':'object','properties':{
        'goal':{'type':'string','minLength':1,'description':TEXT_LIMIT},
        'criterion':{'type':'string','minLength':1,'description':TEXT_LIMIT},
        'tool':name,'tools':names,'segmented':{'type':'boolean','description':'大型作品从第一段起以游标分片写作'},
        'file_access':{'type':'string','enum':['directory','content'],'description':'会话文件目标：查目录或读原件'}},'required':['goal'],'additionalProperties':False}}
    if lookup_schemas:
        steps['items']['properties']['lookup']={'anyOf':[schema.model_json_schema() for schema in lookup_schemas]}
    properties={'tools':names,'steps':steps}
    if allow_reply:
        properties['reply'] = {'type':'string','description':'已有足够信息时的完整最终答复。仅在 tools 和 steps 均为空时填写。'}
    if revise:
        properties['continue_draft']={'type':'boolean','description':'当前草稿满足补充要求，继续从原游标写作'}
    form={'name':'plan_task','description':'为当前需要选择最小工具集，并分解可验证的交付。'+TEXT_LIMIT,'parameters':{
        'type':'object','properties':properties,'required':['steps'],'additionalProperties':False}}
    config={**settings,'_tools':[form],'_batch_tools':False,'_optional_tools':False,'_call_purpose':'task_planning'}
    prompt=(system+'\n你担任当前分身的工具披露步骤。根据本轮用户的实际目标、已有证据和工具目录决定。'
            'tools 只选实际需要的工具。仅需文字或 Mermaid 且已有足够信息时，不规划生成文件或检索。协议要求的会话动作仍须规划。'
            '复杂任务拆为可核对的必要目标，给出 goal 和 criterion。先核对，再决定是否需要追加修订。'
            '可用同一子目标的 tools 列出替代工具。先取证再创作时，将取证与交付分别列为前后两个子目标。'
            '规划只选择一次终态动作，作为独立最后一步。需要发送最后发言并结束时，只规划 end_task，可附发言。'
            '普通网页或动画如涉及复杂场景、交互或较长源码，可选 segmented=true 从第一段开始分段写作。'
            '简单单次动作可只写 goal 和 tool。已确定的只读参数可以填 lookup；有依赖或不确定时留待执行。'
            '每个独立作品只列一项生成目标。生成回执已经证明文件存在，无需另列目录查询或交付确认。只有用户要求检查内容时才规划复查。'
            '当前授权目录只列摘要；不可选择目录外工具。已关闭的能力如确实需要，可选择其 ID 以提示用户。'
            '调用 plan_task 或返回相同结构的 JSON。'+json.dumps(catalog,ensure_ascii=False))
    if allow_reply:
        prompt += ('信息充分且无需工具时，直接在 reply 写本轮完整正文或 Mermaid，tools 和 steps 留空。'
                   '凡需查证、询问、生成、修改或交付文件、执行操作时，规划所需工具，不写 reply。'
                   '正文只写结果和解释，禁止声称已执行未取得回执的操作，不写内部计划、HTML 源码或沙箱脚本。')
    else:
        prompt += '本步只给出工具和目标，最终发言由执行阶段提交。'
    if 'read_session_file' in catalog:
        prompt+='read_session_file 用 query 查目录，用 file_id 读原件。仅需目录时为目标设置 file_access=directory；需要正文时用 content，目录只是定位。修订作品时先读原件，再提交局部修改。'
    if revise:
        prompt+='只规划剩余目标；说明是否保留当前草稿。'
    usage=None
    overflow=False
    issue = '未得到有效披露'
    for attempt in range(2):
        await check_execution_async(validate)
        options={**config,'_tools':[]} if attempt and (overflow or 'end_task' not in catalog) else config
        spec=prompt if not attempt else prompt+'\n上次失败（数据）：'+json.dumps(issue,ensure_ascii=False)+'。请修正后用简洁的 JSON 返回。'
        room = max(0, input_budget(options)-estimate(spec))
        if overflow:
            room //= 2
        focus = bounded_excerpt(question,room)
        originals = explicit_file_context(options,spec,focus)
        if not originals and options.get('_explicit_session_files'):
            originals = explicit_file_context(options,spec,'',limit=room//2)
        focus = bounded_excerpt(focus,max(0,room-estimate(originals))) + originals
        rows,msg=fit_turn(options,spec,history[-2:] if overflow else history,focus,[])
        try:
            check_context(options,spec,rows,msg)
        except HTTPException:
            overflow=True
            continue
        try:
            result=await call_with_retry(lambda:model.generate(options,key,spec,rows,msg),validate,retry_invalid=False)
        except HTTPException as exc:
            usage=accumulate_usage(usage,getattr(exc,'model_usage',None))
            if getattr(exc,'stop_reason',None)=='context_overflow':
                overflow=True
                continue
            if not any(getattr(exc,k,False) for k in ('invalid_tool_call','empty_model_output','output_limited')):
                raise
            if getattr(exc,'recovery_attempted',False):
                break
            continue
        usage=accumulate_usage(usage,result.usage)
        try:
            if stop_reason(result.finish_reason)=='output_limit':
                continue
            if result.tool_calls:
                if len(result.tool_calls)!=1 or result.tool_calls[0]['name']!='plan_task':
                    continue
                proposal=result.tool_calls[0]['arguments']
            else:
                proposal=json.loads(result.text.strip().removeprefix('```json').removesuffix('```'))
            return validate_disclosure(proposal,catalog,lookups=lookups,revise=revise,allow_reply=allow_reply),usage
        except (ValueError,TypeError) as error:
            issue=bounded_excerpt(str(error),120)
    return None,usage

UPDATE_TASK = {'name':'update_task','description':'提交已达到验收条件的子目标结果；使用工具的目标需要引用已验证的回执编号。',
    'parameters':{'type':'object','properties':{'goal_no':{'type':'integer','minimum':1},
        'result':{'type':'string','minLength':1,'description':TEXT_LIMIT},'receipts':{'type':'array','items':{'type':'integer','minimum':1}}},
        'required':['goal_no','result'],'additionalProperties':False}}


def needs_confirmation(step):
    # A single saved artifact is already verified by the artifact tool. Composite goals need their own result.
    names=step.get('tools',())
    return (not step['tool'] or len(names)>1 or
            bool(step.get('criterion') and step['tool'] not in ('create_html','deliver_material','list_avatars',
                                                                  'send_material','send_public_material','contact_avatar',
                                                                  'end_task','send_message','wait_for_user','ask_user')))


# These actions require a concrete file or a validated handoff before the goal can close.
DELIVERABLES = {'create_html','deliver_material','send_material',
                'send_public_material','contact_avatar'}


# Alternative read methods can satisfy the evidence requirement of a composite delivery.
EVIDENCE_TOOLS = {'search_materials','read_material','find_material','find_public_material',
                  'read_session_file','list_avatars'}
READ_ONLY_GOALS = EVIDENCE_TOOLS | {'get_current_time'}
WRITING_CONTROLS = READ_ONLY_GOALS | {'ask_user','read_step_receipt'}


def read_goals_verified(pending, receipts):
    """A model's final answer can close single-tool reads backed by this plan's real evidence."""
    verified = {(receipt.get('goal_no'), receipt['tool'], receipt.get('mode')) for receipt in receipts}
    for step in pending:
        tool = step['tool']
        if tool not in READ_ONLY_GOALS or any(name != tool for name in step.get('tools', ())):
            return False
        mode = step.get('file_access', 'content') if tool == 'read_session_file' else None
        if (step['goal_no'], tool, mode) not in verified:
            return False
    return bool(pending)


def confirm_goal(args, pending, log, revision):
    number, result, ids = args.get('goal_no'), args.get('result'), args.get('receipts',[])
    goal = next((s for s in pending if s['goal_no']==number),None)
    if (set(args)-{'goal_no','result','receipts'} or type(number) is not int or goal is None or
            not needs_confirmation(goal) or not isinstance(result,str) or not result.strip() or not fits_text_budget(result)):
        raise ValueError('提交结果时须填写仍需核对的目标编号和结果')
    if not isinstance(ids,list) or any(type(i) is not int or not 1<=i<=len(log) for i in ids):
        raise ValueError(f'回执编号必须使用清单中的整数编号，当前共有 {len(log)} 条回执')
    required = set(goal.get('tools',())) | ({goal['tool']} if goal['tool'] else set())
    evidence = {r['tool'] for i in ids for r in (log[i-1],)
                if r.get('goal_no')==number and r.get('input_id')==revision}
    deliveries = required & DELIVERABLES
    primary = goal['tool']
    reads = required & EVIDENCE_TOOLS
    if ((required and not required.intersection(evidence)) or (deliveries and not deliveries.intersection(evidence))
            or (primary in deliveries and primary not in evidence)
            or (deliveries and reads and not reads.intersection(evidence))):
        raise ValueError('缺少该子目标的实际工具回执')
    pending.remove(goal)
    log.append({'id':len(log)+1,'tool':'update_task','goal_no':number,'result':result.strip(),
                'refs':ids,'input_id':revision})
    return '已完成子目标：'+goal['goal']+'。已确认的子结果：'+result.strip()


def action_ready(name, pending, receipts, revision):
    """A planned delivery waits for earlier goals and for evidence in its own composite goal."""
    if name not in DELIVERABLES or not pending:
        return True
    current = pending[0]
    if name not in (current['tool'], *current.get('tools', ()), *current.get('support', ())):
        return False
    verified = {r['tool'] for r in receipts if r.get('goal_no')==current['goal_no'] and r.get('input_id')==revision}
    if name in verified:
        return False
    reads = set(current.get('tools', ())) & EVIDENCE_TOOLS
    if reads and not reads.intersection(verified):
        return False
    return name == current['tool'] or not current['tool'] or current['tool'] in verified


class PlanUnavailable(ValueError):
    """A saved plan references a tool outside the current authorization."""


@dataclass
class ToolPlan:
    plan: list
    pending: list
    selected: list
    blocked: str | None = None
    reply: str | None = None


async def prepare_plan(model, context, question, state, draft, *, allowed, disabled, choices,
                       planned_arguments, changed, proposed, trace, validate, account, allow_reply):
    settings, key, system, history = context
    plan, pending, selected = [], [], []
    if 'ask_user' not in allowed and any(
        'ask_user' in (step.get('tool'),*step.get('tools',())) for step in state.get('remaining', ())
    ):
        state.pop('plan', None)
    if 'plan' in state and not changed:
        plan = state['plan'] or []
        pending = [step for step in state.get('remaining', ()) if not final_reply_step(step)]
        legacy = [n for step in (plan + pending) for n in [step.get('tool'), *step.get('tools',())] if n]
        selected = list(state.get('disclosed_tools', legacy))
        if draft.active and state['draft'].get('tool'):
            selected.append(state['draft']['tool'])
        for step in pending:
            for n in [step.get('tool'),*step.get('tools',())]:
                if n in disabled:
                    return ToolPlan(plan, pending, selected, blocked=n)
                if n and n not in allowed:
                    raise PlanUnavailable('此前所需能力已不可用，请核对当前会话授权')
        selected = list(dict.fromkeys(n for n in selected if n in allowed))
    else:
        progress_data = {'remaining':state.get('remaining', []),
                         'draft':{k:v for k,v in (state.get('draft') or next(iter(state.get('parked_drafts') or ()), {})).items() if k in ('kind','tool','mode','offset')}}
        planning_question = question
        if changed:
            planning_question += '\n先前已确认进度（数据）：'+json.dumps(progress_data,ensure_ascii=False)
        revise = changed and bool(draft.active or state.get('parked_drafts'))
        with trace.step('reason', {'operation':'disclose_tools','tools':list(choices),'history_turns':len(history)}) as decision:
            proposed = proposed if not changed else None
            if proposed is not None:
                try:
                    disclosure = validate_disclosure(proposed, choices, lookups=planned_arguments)
                except ValueError:
                    disclosure = None
            else:
                disclosure, cost = await disclose_tools(model,settings,key,system,history,
                    planning_question,choices,validate,lookups=planned_arguments,revise=revise,allow_reply=allow_reply and not revise)
                account(cost)
            if disclosure is None:
                return None
            selected = disclosure['tools']
            planned = disclosure['steps']
            decision.update(tools=selected,goals=[s['goal'] for s in planned])
            if reply := disclosure.get('reply'):
                decision['decision'] = '已有足够信息，直接作答'
                return ToolPlan(plan, pending, selected, reply=reply)
        keep_draft = disclosure.get('continue_draft',False)
        for name in selected:
            if name in disabled:
                return ToolPlan(plan, pending, selected, blocked=name)
        plan = planned
        if revise:
            draft.revise_goals(planned,keep_draft)
        pending = [dict(step, goal_no=index) for index,step in enumerate(planned,1)]
        plan = [dict(step) for step in planned]
        selected = list(dict.fromkeys([*selected,*(s['tool'] for s in planned if s['tool'])]))
    return ToolPlan(plan, pending, selected)
