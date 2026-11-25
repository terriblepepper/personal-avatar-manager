"""Chat and conversation request contracts."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from avatar.core.text_budget import AgentText


# 职责：定义本人分身或 Avatar 助手问答请求。
# 输入：message 文本按 UTF-8 计量，最大 64 KiB，channel 仅 assistant/avatar，conversation_id 可选。
# 结果与约束：只承载用户消息和会话 ID，不能由浏览器声明可信平台调用者或任意目标账户身份。
class ChatRequest(BaseModel):
    request_id: str | None = Field(default=None, max_length=80)
    personal: StrictBool = False
    message: AgentText
    channel: str = Field(default="assistant", pattern="^(assistant|avatar)$")
    conversation_id: str | None = Field(default=None, max_length=80)
    attachment_ids: list[str] = Field(default_factory=list,max_length=5)
    question_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    agent_pause_id: str | None = Field(default=None, min_length=1, max_length=80)
    agent_control: Literal['continue','retry','end'] | None = None

    @model_validator(mode='after')
    def validate_agent_control(self):
        if self.question_id and (self.agent_control != 'continue' or not self.message.strip()):
            raise ValueError('回答分身提问时请填写补充内容')
        if not self.message and self.agent_control != 'continue':
            raise ValueError('请填写消息')
        if bool(self.agent_pause_id) != bool(self.agent_control) or self.agent_control and not self.conversation_id:
            raise ValueError('控制暂停任务需同时指定会话、暂停标识和操作')
        return self


# 职责：定义创建一个新 Web 对话会话的参数。
# 输入：channel 选择助手或本人分身，title 为 1–80 字符。
# 结果与约束：会话所属账户和目标由认证上下文与业务服务确定。
class ConversationRequest(BaseModel):
    channel: Literal['assistant', 'avatar'] = 'avatar'
    title: str = Field(default='新会话', min_length=1, max_length=80)


class ConversationTitleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=80)
