"""任务分身的原生函数接口；任务身份由当前执行上下文绑定，模型不能指定。"""
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from avatar.core.text_budget import AgentText
from avatar.conversation.collaboration.intent import declaration


class MessageArguments(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    content:str=Field(min_length=1,max_length=60000)


class EndArguments(MessageArguments):
    content:str=Field(default='',max_length=60000)
    reason:AgentText=Field(min_length=1)


class WaitArguments(EndArguments):
    who:Literal['initiator','target','both']


ARGUMENTS={'send_message':MessageArguments,'end_task':EndArguments,'wait_for_user':WaitArguments}
TASK_TOOLS=[
    declaration('send_message','继续本任务，向对方分身发送本轮发言。',MessageArguments.model_json_schema()),
    declaration('end_task','本任务已完成、无新事项或对方告别时调用；立即结束，不再互相确认至轮次上限。可附最后发言及结束原因，结果返回各自主分身记忆。',EndArguments.model_json_schema()),
    declaration('wait_for_user','确实需要某方本人补充意图时暂停任务，并指定待确认方；不表示拒绝。',WaitArguments.model_json_schema()),
]


def task_tool_result(calls):
    """校验唯一调用及参数；返回内容和生命周期状态，不从正文推断意图。"""
    try:
        if len(calls)!=1:raise ValueError()
        call=calls[0];name=call['name'];args=ARGUMENTS[name].model_validate(call['arguments'])
    except (KeyError,TypeError,ValueError,ValidationError):
        raise HTTPException(502,'任务分身未返回有效的函数调用或参数，未继续自动聊天') from None
    state='ended' if name=='end_task' else 'awaiting_input' if name=='wait_for_user' else 'queued'
    required={'initiator':1,'target':2,'both':3}[args.who] if name=='wait_for_user' else 0
    return args.content,state,getattr(args,'reason',''),required,{'name':name,'arguments':args.model_dump()}
