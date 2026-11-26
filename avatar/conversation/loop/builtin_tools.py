"""Bind present conversation capabilities to atomic ReAct tools."""
from pydantic import BaseModel, ConfigDict

from avatar.conversation.collaboration.intent import CONTACT_AVATAR, ContactArguments
from avatar.conversation.collaboration.tools import ARGUMENTS
from avatar.conversation.loop.completion import FinishGate
from avatar.conversation.loop.context import READ_STEP_RECEIPT
from avatar.conversation.loop.drafts import APPEND_HTML_DRAFT, AppendHtmlDraft
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
    registry.add(READ_STEP_RECEIPT, context.read_receipt)

    for definition in config.get('_entry_tools', ()):
        name = definition['name']
        handler = config.get('_react_handlers', {}).get(name)
        if handler is None:
            async def handler(args, name=name):
                return await config['_execute_entry_tool'](name, args)
        registry.add(definition, handler, checkpoint=name in ('run_python','send_public_material'))

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

        registry.add(CREATE_HTML, create, WebPage, checkpoint=True)
        if config.get('_load_session_source'):
            registry.add(EDIT_HTML, edit, EditHtml, checkpoint=True)
        registry.add(APPEND_HTML_DRAFT, append, AppendHtmlDraft, checkpoint=True)

    if delivery:
        registry.add(DELIVERY_TOOLS[0], delivery.execute, FindMaterial)
        registry.add(DELIVERY_TOOLS[1], delivery.execute, SendMaterial, checkpoint=True)
        if conversation := config.get('_personal_conversation'):
            async def contact(args):
                document = await run_blocking(chat.dialogues.intent.loop_draft, config['_material_owner'],
                                              conversation, question, args.model_dump(), config.get('_generated_files', ()))
                return ToolOutcome({'status':'awaiting_owner_confirmation'},
                                   action={'name':'contact_avatar','arguments':document})
            registry.add(CONTACT_AVATAR, contact, ContactArguments, control=True)

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
