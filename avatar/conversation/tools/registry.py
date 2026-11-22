# 模块说明：会话工具目录、授权设置及模型可见状态；新增工具时在目录注册，不接收客户端代码或工具定义。
import json
from fastapi import HTTPException
from avatar.core.db import dump, now
from avatar.models.effort import levels

TOOL_CATALOG = {
    'python': {'name': 'Python 沙箱', 'description': '计算、分析数据、绘制图表、处理选中文件并生成下载文件。支持 NumPy、pandas、Matplotlib、Pillow 和 pypdf。', 'default': False},
    'animation': {'name': '制作动画', 'description': '开启时同时授权当前会话使用 Python 沙箱。分身每轮制作 HTML 动画。默认 2D，需要立体演示时使用 3D。', 'default': False},
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
        return {'items':[{'id':key,**value,'enabled':saved.get(key,value['default']) is True} for key,value in TOOL_CATALOG.items()],
                'updated_at':row['tools_updated_at'], 'reasoning_effort':saved.get('reasoning_effort')}

    def effort(self, user_id, conversation_id, *, selected=...):
        with self.files.database.connect(write=selected is not ...) as db:
            row = self.files.session(db, user_id, conversation_id)
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
        with self.files.database.connect(write=True) as db:
            row=self.files.session(db,user_id,conversation_id,editable=True)
            saved=json.loads(row['tools_config'])
            if settings.get('animation') is True:
                saved['python'] = True
            saved.update(settings)
            db.execute('UPDATE chat_conversations SET tools_config=?,tools_updated_at=? WHERE id=?',(dump(saved),now(),conversation_id))
        return self.get(user_id,conversation_id)
