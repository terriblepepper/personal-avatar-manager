"""Bind present conversation capabilities to atomic ReAct tools."""
from pydantic import BaseModel, ConfigDict

from avatar.conversation.collaboration.intent import CONTACT_AVATAR, ContactArguments
from avatar.conversation.collaboration.tools import ARGUMENTS
from avatar.conversation.loop.completion import FinishGate
from avatar.conversation.loop.context import READ_STEP_RECEIPT
from avatar.conversation.loop.drafts import APPEND_HTML_DRAFT, AppendHtmlDraft
from avatar.conversation.loop.entry_tools import is_session_html
from avatar.conversation.loop.registry import ToolRegistry
from avatar.conversation.loop.types import ToolOutcome
from avatar.conversation.tools.builtin import CURRENT_TIME, beijing_time
from avatar.conversation.tools.html import CREATE_HTML, WebPage
from avatar.conversation.tools.html_revision import EDIT_HTML, EditHtml, expand_revision
from avatar.conversation.tools.material_delivery import DELIVERY_TOOLS, FindMaterial, SendMaterial
from avatar.core.execution import AskUser, run_blocking


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


def bind_tools(chat, config, question, validate, trace, context, drafts, delivery, policy):
    registry = ToolRegistry(validate, trace)
    gate = FinishGate(config, policy, delivery, drafts)

    async def clock(_):
        return beijing_time()
    registry.add(CURRENT_TIME, clock, EmptyArguments)
    registry.add(READ_STEP_RECEIPT, context.read_receipt, disclosure='state',
                 when=lambda: bool(context.receipt_refs))

    def has_public_targets():
        ready = config.get('_public_material_ready')
        return ready is not None and ready()

    def has_material_targets():
        return bool(config.get('_visible_material_keys') or config.get('_recent_material_ids')
                    or config.get('_material_sources') or delivery and delivery.visible_ids or has_public_targets())

    def has_session_files():
        return bool(config.get('_has_session_files') or config.get('_explicit_session_files')
                    or config.get('_agent_saved_files') or config.get('_generated_files'))

    def has_session_html():
        return bool(config.get('_has_session_html')) or any(
            is_session_html(item) for files in (config.get('_explicit_session_files', ()),
                config.get('_agent_saved_files', ()), config.get('_generated_files', ()),
                config.get('_session_read_proofs', {}).values()) for item in files
        )

    state_tools = {'read_material': has_material_targets, 'read_session_file': has_session_files,
                   'send_public_material': has_public_targets}
    summaries = {'run_python':'沙箱计算、分析和绘图',
                 'find_public_material':'查找公开资料原件', 'list_avatars':'查找平台公开分身',
                 'application_status':'查询分身申请进度'}

    for definition in config.get('_entry_tools', ()):
        name = definition['name']
        handler = config.get('_react_handlers', {}).get(name)
        if handler is None:
            async def handler(args, name=name):
                return await config['_execute_entry_tool'](name, args)
        when = state_tools.get(name)
        summary = summaries.get(name, '')
        disclosure = 'state' if when else 'deferred' if summary else 'core'
        registry.add(definition, handler, checkpoint=name in ('run_python','send_public_material'),
                     disclosure=disclosure, summary=summary, when=when)

    async def terminal(args, definition):
        name = definition['name']
        minimum = definition['parameters'].get('properties', {}).get('content', {}).get('minLength', 0)
        if len(args.content)<minimum:
            return ToolOutcome.error('请在 content 中提供对最新补充的实际回复，再结束任务。')
        action = {'name':name, 'arguments':args.model_dump()}
        if error := gate.check(args.content, action):
            return ToolOutcome.error(error)
        return ToolOutcome({'status':'confirmed'}, action=action)

    for definition in config.get('_tools', ()):
        name = definition['name']
        if name in ARGUMENTS:
            async def action(args, definition=definition):
                return await terminal(args, definition)
            registry.add(definition, action, ARGUMENTS[name], control=True)

    if config.get('_allow_user_questions', True):
        async def ask(args):
            pending = args.pending()
            return ToolOutcome({'status':'waiting_for_user','question_id':pending['id']},
                               action={'name':'ask_user','arguments':pending})
        registry.add({'name':'ask_user', 'description':'仅在任务缺少必要信息时发起一个具体提问，可附最多三个建议。',
                      'parameters':AskUser.model_json_schema()}, ask, AskUser, control=True)

    if config.get('_html_enabled'):
        async def create(args):
            return await drafts.artifact(args.model_dump())

        async def edit(args):
            expanded = await run_blocking(expand_revision, args, config['_load_session_source'])
            return await drafts.artifact(expanded)

        async def append(args):
            return await drafts.append(args.model_dump())

        registry.add(CREATE_HTML, create, WebPage, checkpoint=True,
                     disclosure='core' if policy.require_html else 'deferred', summary='生成网页或 HTML 动画')
        if config.get('_load_session_source'):
            registry.add(EDIT_HTML, edit, EditHtml, checkpoint=True, disclosure='state', when=has_session_html)
        registry.add(APPEND_HTML_DRAFT, append, AppendHtmlDraft, checkpoint=True, disclosure='state',
                     when=lambda: bool(drafts.active))

    if delivery:
        def has_send_targets():
            return bool(delivery.visible_ids or config.get('_recent_material_ids') or
                        any(source == 'profile' for source, _ in config.get('_visible_material_keys', ())) or
                        any(delivery.known_original(item['file_id']) for item in config.get('_material_sources', ())))

        registry.add(DELIVERY_TOOLS[0], delivery.execute, FindMaterial, disclosure='deferred', summary='查找本人资料库原件')
        registry.add(DELIVERY_TOOLS[1], delivery.execute, SendMaterial, checkpoint=True, disclosure='state',
                     when=has_send_targets)
        if conversation := config.get('_personal_conversation'):
            async def contact(args):
                document = await run_blocking(chat.dialogues.intent.loop_draft, config['_material_owner'],
                                              conversation, question, args.model_dump(), config.get('_generated_files', ()))
                return ToolOutcome({'status':'awaiting_owner_confirmation'},
                                   action={'name':'contact_avatar','arguments':document})
            registry.add(CONTACT_AVATAR, contact, ContactArguments, control=True,
                         disclosure='deferred', summary='起草分身消息或任务')

    disabled = {item['id']:item['name'] for item in config.get('_conversation_tools', ())
                if item['id']!='animation' and not item['enabled']}
    if disabled:
        async def require(args):
            tool = args.get('tool_id')
            if set(args)!={'tool_id'} or not isinstance(tool,str) or tool not in disabled:
                return ToolOutcome.error('请选择当前确实需要且尚未开启的工具。')
            notice = f"请在当前会话的「工具 → {disabled[tool]}」中开启后继续。"
            return ToolOutcome({'status':'tool_required'},
                               action={'name':'require_conversation_tool','arguments':{'content':notice}})
        registry.add({'name':'require_conversation_tool','description':'提示用户开启当前确实需要的已关闭工具。',
                      'parameters':{'type':'object','properties':{'tool_id':{'type':'string','enum':list(disabled)}},
                                    'required':['tool_id'],'additionalProperties':False}}, require, control=True)
    return registry, gate
