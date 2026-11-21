# 模块说明：公开目录与旧助手子会话存储；内部旧名称用于兼容历史子会话，与真实叮当平台通信分离。
"""Public directory search and legacy child-session storage; model tools choose intent."""
import json
import re
import unicodedata
import uuid
from avatar.profiles.profile_fields import public_profile
from avatar.core.db import now, dump


# 职责：统一分身名称和用户输入的匹配格式。
# 输入：text 为名称、AIC 或消息字符串。
# 结果与约束：执行 NFKC、大小写折叠及首尾去空白，返回用于规则检索的字符串。
def normalized(text):
    return unicodedata.normalize('NFKC',text).casefold().strip()


# 职责：维护模型工具调用可用的公开目标目录。
# 输入：助手模块名称与 Web 一致；历史 platform=dingdang 标识仅用于兼容已存会话，不代表真实平台接入。
# 结果与约束：只检索已激活账户的公开分身别名，支持指定、追问、返回及多目标澄清。
class AvatarAssistantRouter:
    # 职责：保存目标目录与子会话所用数据库。
    # 输入：database 为应用共享存储。
    # 结果与约束：不在构造时加载目标，避免缓存已变更的审核资料。
    def __init__(self, database):self.database=database

    # 职责：生成当前可调度分身及其公开别名集合。
    # 输入：从 agents 与已激活 users 查询，使用 public_profile 过滤身份资料。
    # 结果与约束：返回含 AIC、显示名、规范化别名及内部行的目标列表；无公开名称的目标不参与名称调度。
    def catalog(self):
        with self.database.connect() as db:
            rows=db.execute("SELECT a.* FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE u.account_status='active'").fetchall()
        result=[]
        for row in rows:
            public=public_profile(json.loads(row['profile']))
            aliases={normalized(public[k]) for k in ('name','avatar_name','preferred_name') if public.get(k)}
            if not aliases:continue
            result.append({'row':row,'aliases':aliases,'aic':row['aic'],'name':public.get('avatar_name') or public.get('name') or public.get('preferred_name')})
        return result

    # 目录查询只执行已由模型选定的工具，不从自然语言判断意图。
    def catalog_reply(self,query='',offset=0):
        words=normalized(query).split()
        targets=[t for t in self.catalog() if all(word in ' '.join([normalized(t['name']),normalized(t['aic']),*t['aliases']]) for word in words)]
        offset=min(offset,((max(len(targets),1)-1)//10)*10)
        return ('平台公开分身（共 '+str(len(targets))+' 个，第 '+str(offset//10+1)+' 页，每页 10 个）：\n'+'\n'.join(f"- {t['name']}（AIC：{t['aic']}）" for t in targets[offset:offset+10])) if targets else '没有找到符合条件的公开分身，可修改名称或 AIC 重新查询。'

    # 职责：在公开目录中匹配姓名、别名或 AIC，供助手问答和协商共用。
    # 输入：message 为用户文本，targets 为可选目录子集。
    # 结果与约束：ASCII 使用单词边界；不加载私密别名，不缓存目录。
    def matches(self, message, targets=None):
        text=normalized(message)
        return [target for target in (self.catalog() if targets is None else targets)
                if normalized(target['aic']) in text or any(
                    bool(re.search(r'(?<![a-z0-9_])'+re.escape(alias)+r'(?![a-z0-9_])',text))
                    if alias.isascii() else alias in text for alias in target['aliases'])]

    # 职责：取得父会话中对应调用者和目标的内部子会话。
    # 输入：memory 为记忆服务，caller_id 为已认证调用者，parent 为父会话，target 为已选分身。
    # 结果与约束：稳定 UUID 隔离目标记忆，保存 parent_id 与选中目标；内部 platform 标识阻止经 Web 会话接口直接越权访问。
    def child(self, memory, caller_id, parent, target):
        cid=str(uuid.uuid5(uuid.NAMESPACE_URL,dump(['dingdang',parent['id'],caller_id,target['aic']])))
        # 内部目标子会话使用不同平台作用域，不能通过公开 Web 会话 API 直接访问。
        with self.database.connect() as db:
            # Demo 迁移后目标 AIC 改变但原子会话 ID 保留，按完整归属复用旧会话及其记忆。
            existing = db.execute("SELECT id FROM chat_conversations WHERE parent_id=? AND user_id=? AND caller_id=? AND channel='assistant' AND platform='dingdang' AND target_aic=? ORDER BY created_at LIMIT 1",
                                  (parent['id'], caller_id, caller_id, target['aic'])).fetchone()
            if existing:
                cid = existing['id']
            exists=db.execute('SELECT 1 FROM chat_conversations WHERE id=?',(cid,)).fetchone()
        child=memory.scope(caller_id,'assistant',target['aic'],cid,caller_id=caller_id,
                           platform='dingdang',create=not bool(exists),title=target['name'])
        with self.database.connect(write=True) as db:
            db.execute('UPDATE chat_conversations SET parent_id=? WHERE id=?',(parent['id'],cid))
            db.execute('UPDATE chat_conversations SET selected_aic=?,updated_at=? WHERE id=?',(target['aic'],now(),parent['id']))
        child['parent_id']=parent['id']
        return child
