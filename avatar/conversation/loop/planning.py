"""Disclose authorized tools before execution and retain independently verifiable goals."""
import json

from fastapi import HTTPException

from avatar.core.text_budget import TEXT_LIMIT, fits_text_budget
from avatar.conversation.working_context import bounded_excerpt, fit_turn, input_budget
from avatar.core.tokens import estimate
from avatar.core.execution import AskUser, call_with_retry, check_execution_async
from avatar.models.client import accumulate_usage
from avatar.models.diagnostics import stop_reason
from avatar.models.limits import check_context


def directory_allowed(system):
    try:
        data, _ = json.JSONDecoder().raw_decode(system.split('\n', 1)[1])
        return isinstance(data, dict) and 'platform_avatars' in data
    except (ValueError, IndexError):
        return False


def available_tools(config, system):
    from avatar.conversation.collaboration.intent import TOOLS
    from avatar.conversation.tools.html import CREATE_HTML, animation_tool
    from avatar.conversation.tools.material_delivery import DELIVERY_TOOLS
    from avatar.conversation.loop.context import READ_STEP_RECEIPT

    domain = config.get('_tools', ())
    allowed = {tool['name']: tool for tool in domain}
    allowed.update((t['name'],t) for t in config.get('_entry_tools', ()))
    owner = config.get('_material_owner')
    personal = owner and not domain and config.get('_personal_conversation')
    if owner:
        allowed.update((tool['name'], tool) for tool in DELIVERY_TOOLS)
    if config.get('_html_enabled'):
        allowed.update(create_html=CREATE_HTML, create_animation=animation_tool())
    for tool in TOOLS:
        name = tool['name']
        if ((name == 'list_avatars' and directory_allowed(system))
                or (name == 'continue_answer' and (not domain or config.get('_optional_tools')))
                or (name == 'deliver_material' and owner)
                or (personal and name in ('find_conversation_files','contact_avatar'))):
            allowed[name] = tool
    disabled = {t['id']: t for t in config.get('_conversation_tools', ()) if t['id'] != 'animation' and not t['enabled']}
    allowed['ask_user'] = {'name':'ask_user', 'description':'需要用户补充时提出一个具体问题。可给最多三个简短建议，用户也可自由回答。',
        'parameters':AskUser.model_json_schema()}
    if disabled:
        allowed['require_conversation_tool'] = {'name':'require_conversation_tool','description':'提示用户开启当前确实需要的已关闭工具。',
            'parameters':{'type':'object','properties':{'tool_id':{'type':'string','enum':list(disabled)}},
                          'required':['tool_id'],'additionalProperties':False}}
    allowed['read_step_receipt'] = READ_STEP_RECEIPT
    return allowed


def disclosure_catalog(config, system):
    catalog = {name:' '.join(tool['description'].split())[:220] for name,tool in available_tools(config, system).items()}
    if 'create_html' in catalog:
        catalog.update(create_html='创建独立的普通 HTML 网页。',
                       create_animation='自由绘制并交付可播放、暂停、拖动时间轴的离线二维或三维 HTML 动画。')
    catalog.update({t['id']:t['name']+'（已关闭）' for t in config.get('_conversation_tools', ())
                    if t['id'] != 'animation' and not t['enabled']})
    return catalog


def _selected(value, choices):
    if not isinstance(value,list) or len(value)>len(choices) or any(not isinstance(n,str) or n not in choices for n in value):
        raise ValueError('选择了当前目录之外的工具')
    return list(dict.fromkeys(value))


def validate_disclosure(value, choices, *, revise=False):
    from avatar.conversation.tools.material_delivery import FindMaterial
    fields = {'tools','steps','continue_draft'} if revise else {'tools','steps'}
    if not isinstance(value,dict) or set(value)-fields or not isinstance(value.get('steps'),list):
        raise ValueError('披露缺少子目标')
    if len(value['steps'])>16:
        raise ValueError('单次披露最多十六项子目标，可后续补充')
    selected = _selected(value.get('tools',[]),choices)
    steps=[]
    text_parts=[]
    for source in value['steps']:
        if not isinstance(source,dict) or set(source)-{'goal','tool','tools','criterion','lookup','html_mode','segmented'}:
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
        if 'html_mode' in s and (primary!='create_animation' or s['html_mode'] not in ('2d','3d')):
            raise ValueError('动画维度不合法')
        if 'segmented' in s and (type(s['segmented']) is not bool or primary not in ('create_html','create_animation')):
            raise ValueError('只有作品子目标支持分段写作')
        if 'lookup' in s:
            if primary not in ('find_material','find_animation','find_public_material'):
                raise ValueError('该子目标不支持检索参数')
            s['lookup']=FindMaterial.model_validate(s['lookup']).model_dump(exclude_unset=True)
        selected.extend(n for n in [primary,*tools] if n and n not in selected)
        text_parts.extend((s['goal'], s.get('criterion',''), s.get('lookup',{}).get('query','')))
        steps.append(s)
    if not fits_text_budget(*text_parts):
        raise ValueError(TEXT_LIMIT)
    if type(value.get('continue_draft',False)) is not bool:
        raise ValueError('计划规模或草稿决定无效')
    ret={'tools':selected,'steps':steps}
    if revise:
        ret['continue_draft']=value.get('continue_draft',False)
    return ret


async def disclose_tools(model, settings, key, system, history, question, catalog, validate, *, revise=False):
    from avatar.conversation.tools.material_delivery import FindMaterial
    name={'type':'string','enum':list(catalog)}
    names={'type':'array','items':name,'uniqueItems':True}
    steps={'type':'array','maxItems':16,'items':{'type':'object','properties':{
        'goal':{'type':'string','minLength':1,'description':TEXT_LIMIT},
        'criterion':{'type':'string','minLength':1,'description':TEXT_LIMIT},
        'tool':name,'tools':names,'segmented':{'type':'boolean','description':'大型作品从第一段起以游标分片写作'},'lookup':FindMaterial.model_json_schema(),
        'html_mode':{'type':'string','enum':['2d','3d']}},'required':['goal'],'additionalProperties':False}}
    properties={'tools':names,'steps':steps}
    if revise:
        properties['continue_draft']={'type':'boolean','description':'当前草稿满足补充要求，继续从原游标写作'}
    form={'name':'plan_task','description':'为当前需要选择最小工具集，并分解可验证的交付。'+TEXT_LIMIT,'parameters':{
        'type':'object','properties':properties,'required':['steps'],'additionalProperties':False}}
    config={**settings,'_tools':[form],'_batch_tools':True,'_optional_tools':True,'_call_purpose':'task_planning'}
    prompt=(system+'\n你担任当前分身的工具披露步骤。根据本轮用户的实际目标、已有证据和工具目录决定。'
            'tools 只选可能真正用到的工具 ID；不用工具且可直接回答时给空 tools 和 steps。'
            '复杂任务拆为可分别核对的子目标，给出 goal 和 criterion（具体证据或交付条件）。'
            '可用同一子目标的 tools 列出替代工具。先取证再创作时，将取证与交付分别列为前后两个子目标。'
            '通信、提交发言、等待用户、结束任务属于终态；应作为独立的最后一步。'
            '普通网页或动画如涉及复杂场景、交互或较长源码，可选 segmented=true 从第一段开始分段写作。'
            '简单单次动作可只写 goal 和 tool。资料检索若已确定参数可填 lookup；有依赖时等前一结果再选。'
            '每个独立作品单列交付条件和维度；多个步骤服务于同一作品时只为最终作品列一项。'
            '当前授权目录只列摘要；不可选择目录外工具。已关闭的能力如确实需要，可选择其 ID 以提示用户。'
            '只返回 plan_task 或相同结构的 JSON。'+json.dumps(catalog,ensure_ascii=False))
    if revise:
        prompt+='只规划剩余目标；说明是否保留当前草稿。'
    usage=None
    overflow=False
    for attempt in range(2):
        await check_execution_async(validate)
        options=config if not attempt else {**config,'_tools':[]}
        spec=prompt if not attempt else prompt+'\n上次未得到有效披露，请用简洁的 JSON 重试。'
        focus = bounded_excerpt(question, max(64, input_budget(options) - estimate(spec))) if overflow else question
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
            return validate_disclosure(proposal,catalog,revise=revise),usage
        except (ValueError,TypeError):
            continue
    return None,usage

REQUEST_TOOLS = {'name':'request_tools','description':'现有工具不足或需要补充子目标时，交给同一分身重新披露授权工具。',
    'parameters':{'type':'object','properties':{'need':{'type':'string','minLength':1,'description':TEXT_LIMIT},
        'replace_goals':{'type':'array','items':{'type':'integer','minimum':1},'description':'有新证据需调整的未完成子目标编号。明确交付的作品须保留。'}},
                  'required':['need'],'additionalProperties':False}}
UPDATE_TASK = {'name':'update_task','description':'提交已达到验收条件的子目标结果；使用工具的目标需要引用已验证的回执编号。',
    'parameters':{'type':'object','properties':{'goal_no':{'type':'integer','minimum':1},
        'result':{'type':'string','minLength':1,'description':TEXT_LIMIT},'receipts':{'type':'array','items':{'type':'integer','minimum':1}}},
        'required':['goal_no','result'],'additionalProperties':False}}


def needs_confirmation(step):
    # A single saved artifact is already verified by the artifact tool. Composite goals need their own result.
    names=step.get('tools',())
    return (not step['tool'] or len(names)>1 or
            bool(step.get('criterion') and step['tool'] not in ('create_html','create_animation','deliver_material',
                                                                  'send_material','send_animation','contact_avatar',
                                                                  'end_task','send_message','wait_for_user','ask_user')))


# These actions require a concrete file or a validated handoff before the goal can close.
DELIVERABLES = {'create_html','create_animation','deliver_material','send_material','send_animation',
                'send_public_material','contact_avatar'}


def revise_goals(plan, pending, selected, addition, replace_goals):
    revised = False
    if addition['steps'] and replace_goals:
        keep = [s for s in pending if s['goal_no'] not in replace_goals or
                DELIVERABLES.intersection([s['tool'], *s.get('tools',())])]
        revised = len(keep)!=len(pending)
        pending[:] = keep
    for name in addition['tools']:
        if name not in selected:
            selected.append(name)
            revised = True
    insert = 0
    for step in addition['steps']:
        if not any(s['goal']==step['goal'] and s['tool']==step['tool'] for s in pending):
            plan.append(dict(step))
            pending.insert(insert,dict(step,goal_no=len(plan)))
            insert+=1
            revised = True
    return revised


# Alternative read methods can satisfy the evidence requirement of a composite delivery.
EVIDENCE_TOOLS = {'search_materials','read_material','find_material','find_animation','find_public_material',
                  'find_conversation_files'}
WRITING_CONTROLS = EVIDENCE_TOOLS | {'ask_user','request_tools','read_step_receipt','list_avatars'}


def confirm_goal(args, pending, log, revision):
    number, result, ids = args.get('goal_no'), args.get('result'), args.get('receipts',[])
    goal = next((s for s in pending if s['goal_no']==number),None)
    if (set(args)-{'goal_no','result','receipts'} or type(number) is not int or goal is None or
            not needs_confirmation(goal) or not isinstance(result,str) or not result.strip() or not fits_text_budget(result) or
            not isinstance(ids,list) or any(type(i) is not int or not 1<=i<=len(log) for i in ids)):
        raise ValueError('提交子结果时需要开放的目标编号和有效的回执编号')
    required = set(goal.get('tools',())) | ({goal['tool']} if goal['tool'] else set())
    evidence = {r['tool'] for i in ids for r in (log[i-1],)
                if r.get('goal_no')==number and r.get('input_id')==revision}
    reads = required & EVIDENCE_TOOLS
    if ((required and not required.intersection(evidence)) or not (required & DELIVERABLES).issubset(evidence)
            or (required & DELIVERABLES and reads and not reads.intersection(evidence))):
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
