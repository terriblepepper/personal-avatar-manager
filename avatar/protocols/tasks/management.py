# 模块说明：平台协议按 ACS 技能提供公开目录查询或 Web 入口，不执行账户和配置变更。
import json
import re
from contextlib import nullcontext
from collections.abc import Mapping
from avatar.profiles.profile_fields import public_profile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SKILLS = frozenset({'avatar.manager.profile', 'avatar.manager.discovery', 'avatar.manager.account-link', 'avatar.manager.settings'})


# 职责：校验已注册管理技能的结构化输入。
# 输入：技能标识及保留兼容的查询/分页字段；不接受用户身份、凭据或模型参数。
# 结果与约束：禁止未知字段；查询只读取公开目录，管理操作返回 Web 入口。
class ManagementInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    skillId: str = Field(min_length=1, max_length=100)
    query: str = Field(default='', max_length=200)
    offset: int = Field(default=0, ge=0, le=100000, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)


# 职责：提供系统在小镇接入初期的固定 Web 入口回复。
# 输入：保留运行时传入的 database 参数以兼容装配，base_url 是可信部署地址。
# 结果与约束：仅公开查询读取业务库，不调用模型、绑定服务或令牌签发器。
class SystemManagementService:
    # 职责：保存固定回复使用的 Web 根地址。
    # 输入：database 为保留的装配参数但不使用，base_url 为服务器配置。
    # 结果与约束：保存 URL 与数据库引用，不联网或修改账户、资料及会话。
    def __init__(self, database, base_url):
        self.base_url = base_url.rstrip('/')
        self.database = database

    # 职责：校验协议输入并归一化为已注册管理技能。
    # 输入：TaskCommand.dataItems，仅接受单条结构化指令或纯文本。
    # 结果与约束：目录查询文本直接检索，其余文本返回 Web 入口；未知技能及额外身份参数拒绝。
    @staticmethod
    def parse(items):
        if not isinstance(items, (list, tuple)) or not items or len(items) > 8 or any(not isinstance(item, Mapping) for item in items):
            raise ValueError('请提供文本或管理指令')
        if len(items) == 1 and items[0].get('type') == 'data':
            try:
                command = ManagementInput.model_validate(items[0].get('data'))
            except ValidationError:
                raise ValueError('管理指令字段无效') from None
            if command.skillId not in SKILLS:
                raise ValueError('系统不支持该技能，请通过 Web 页面访问')
            return command
        if any(item.get('type') != 'text' or not isinstance(item.get('text'), str) for item in items):
            raise ValueError('仅支持纯文本或单条结构化管理指令')
        text = '\n'.join(item['text'] for item in items).strip()
        if not text or len(text) > 2000:
            raise ValueError('文本长度应为 1..2000 字符')
        # 纯文本也按公开目录意图分流，个人管理请求仍交给网页。
        if not re.search(r'申请|审核|进度|绑定|配置|资料维护',text) and re.search(r'分身|AIC',text,re.I):
            if re.search(r'哪些|列表|目录|列出|所有|全部|分身名称[和及、\s]*AIC',text,re.I):
                return ManagementInput(skillId='avatar.manager.discovery')
            match=re.fullmatch(r'(?:请(?:帮我)?|帮我)?\s*(?:查询|查找|搜索|检索|查看)\s*(.+?)[。？?！!]*',text)
            if match:
                query=re.sub(r'^(?:公开)?分身(?:名称)?[：:\s]*|的?(?:公开)?分身$','',match[1]).strip(' \"“”\'')
                if len(query)<=200:return ManagementInput(skillId='avatar.manager.discovery',query=query)
        return ManagementInput(skillId='avatar.manager.profile')

    # 职责：公开目录技能返回文本结果，其他管理技能返回 Web URL。
    # 输入：command 是经过结构校验的管理输入，业务参数不用于查询或写入。
    # 结果与约束：仅返回公开姓名、分身名和 AIC；不提供私密资料、绑定结果或自动登录令牌。
    def execute(self, command, *, db=None):
        if command.skillId not in SKILLS:
            raise ValueError('系统不支持该技能')
        if command.skillId=='avatar.manager.discovery':
            with (nullcontext(db) if db is not None else self.database.connect()) as db:
                rows=db.execute("SELECT a.aic,a.profile FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.identity_status='active' AND u.account_status='active' AND a.aic NOT LIKE 'DEMO-%' ORDER BY a.aic").fetchall()
            matches=[]
            for row in rows:
                profile=public_profile(json.loads(row['profile']))
                item={'aic':row['aic'],'name':profile.get('name',''),'avatar_name':profile.get('avatar_name','')}
                if not command.query or any(command.query.casefold() in value.casefold() for value in item.values()):matches.append(item)
            items=matches[command.offset:command.offset+command.limit]
            text='\n'.join(f"{item['name']} / {item['avatar_name']}（{item['aic']}）" for item in items) or '未找到符合条件的公开分身。'
            return {'text':text,'items':items,'total':len(matches)}
        url = self.base_url + '/'
        return {'text': url, 'webAppUrl': url}
