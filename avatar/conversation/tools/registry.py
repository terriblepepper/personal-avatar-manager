# 模块说明：会话工具目录、授权设置及模型可见状态；新增工具时在目录注册，不接收客户端代码或工具定义。
import json
from contextlib import contextmanager
from fastapi import HTTPException
from avatar.core.db import dump, now
from avatar.models.effort import levels
from avatar.models.embedding import EmbeddingSettings

TOOL_CATALOG = {
    'python': {'name': 'Python 沙箱', 'description': '计算、分析数据、绘制图表、处理选中文件并生成下载文件。支持 NumPy、pandas、Matplotlib、Pillow 和 pypdf。', 'default': False},
    'animation': {'name': '制作动画', 'description': '将制作动画作为本轮目标，并授权当前会话使用 Python 沙箱。', 'default': False},
}


# 职责：按当前登录账户和父会话读取或修改工具授权；设置在会话间隔离。
class ConversationTools:
    # 职责：复用文件服务的会话归属与处理中租约校验。
    def __init__(self, files):
        self.files=files

    # 职责：返回目录和开关快照；未知工具不被历史配置意外启用。
    def get(self, user_id, conversation_id):
        with self.files.database.connect() as db:
            row=self.files.session(db,user_id,conversation_id)
            saved=json.loads(row['tools_config'])
            agent_enabled=EmbeddingSettings.configured(db,user_id)
        items=[]
        for key,value in TOOL_CATALOG.items():
            item={'id':key,**value,'enabled':saved.get(key,value['default']) is True}
            if not agent_enabled:
                item.update(enabled=False,locked=True)
            elif row['channel']=='peer' and key=='python':
                item.update(enabled=True,locked=True)
            items.append(item)
        return {'items':items,'updated_at':row['tools_updated_at'],'reasoning_effort':saved.get('reasoning_effort'),'agent_enabled':agent_enabled}

    @contextmanager
    def settings_session(self, user_id, conversation_id, *, write, editable=False):
        with self.files.database.connect() as db:
            row = self.files.session(db,user_id,conversation_id,editable=editable)
            if not write:
                yield db, row
                return
            owner = row['user_id']
        # A shared workspace owned by the peer combines their stored settings with the viewer's model.
        with self.files.database.connect(write=True, account_scope=user_id if owner==user_id else None) as db:
            row = self.files.session(db,user_id,conversation_id,editable=editable)
            if row['user_id'] != owner:
                raise HTTPException(409,'文件工作区已改变，请重试')
            yield db, row

    def effort(self, user_id, conversation_id, *, selected=...):
        with self.settings_session(user_id,conversation_id,write=selected is not ...) as (db,row):
            model = db.execute('SELECT model_config FROM model_settings WHERE user_id=?', (user_id,)).fetchone()
            config = json.loads(model[0]) if model else {}
            available = levels(config)
            saved = json.loads(row['tools_config'])
            if selected is not ...:
                if selected is not None and selected not in available:
                    raise HTTPException(422, '当前模型不支持所选推理强度，请刷新后重试')
                saved['reasoning_effort'] = selected
                db.execute('UPDATE chat_conversations SET tools_config=? WHERE id=?', (dump(saved), conversation_id))
            current = saved.get('reasoning_effort')
            return {'selected': current if current in available else None,
                    'default': config.get('reasoning_effort'), 'levels': available, 'model': config.get('model', '')}

    # 职责：修改注册工具的布尔开关；未知字段、非布尔值和处理中修改均拒绝。
    def update(self,user_id,conversation_id,settings):
        if not settings or any(key not in TOOL_CATALOG or type(value) is not bool for key,value in settings.items()):
            raise HTTPException(422,'请选择有效工具和布尔开关')
        with self.settings_session(user_id,conversation_id,write=True,editable=True) as (db,row):
            agent_enabled=EmbeddingSettings.configured(db,user_id)
            if not agent_enabled and any(settings.values()):
                raise HTTPException(409,'普通聊天模式下工具不可用，请先配置 Embedding 模型')
            if row['channel']=='peer' and agent_enabled:
                if settings.get('python') is False:
                    raise HTTPException(409,'分身间会话的 Python 沙箱始终开启')
                settings={key:value for key,value in settings.items() if key!='python'}
            if settings:
                saved=json.loads(row['tools_config'])
                if row['channel']!='peer' and settings.get('animation') is True:
                    saved['python'] = True
                saved.update(settings)
                db.execute('UPDATE chat_conversations SET tools_config=?,tools_updated_at=? WHERE id=?',(dump(saved),now(),conversation_id))
        return self.get(user_id,conversation_id)
