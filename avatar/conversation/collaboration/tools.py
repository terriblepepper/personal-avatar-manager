"""任务分身的原生函数接口；任务身份由当前执行上下文绑定，模型不能指定。"""
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from avatar.core.text_budget import AgentText
from avatar.conversation.collaboration.intent import declaration


class MessageArguments(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    content:str=Field(min_length=1,max_length=60000)


class DispositionArguments(MessageArguments):
    content:str=Field(default='',max_length=60000)
    reason:AgentText=Field(min_length=1)


class EndArguments(DispositionArguments):
    stop_requested:StrictBool=Field(default=False,description='仅当本人最新补充明确要求结束当前任务时设为 true。')


class WaitArguments(DispositionArguments):
    who:Literal['initiator','target','both']


ARGUMENTS={'send_message':MessageArguments,'end_task':EndArguments,'wait_for_user':WaitArguments}
TASK_TOOLS=[
    declaration('send_message','继续本任务，向对方分身发送本轮发言。',MessageArguments.model_json_schema()),
    declaration('end_task','完成交付或无需后续发言时结束本任务，可在 content 附最终答复。本人最新补充明确要求停止时，可用 stop_requested 结束。',EndArguments.model_json_schema()),
    declaration('wait_for_user','确实缺少本人信息时暂停。只指定需要补充的一方；双方各有缺失信息时才选 both。',WaitArguments.model_json_schema()),
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
