# 模块说明：WIT 负责认证和传输；应用保存管理任务的幂等结果、确认状态与会话归属，不调用模型。
import json
import time
import uuid
from datetime import datetime, timezone


# 职责：表达可以安全返回的管理任务错误。
# 输入：协议错误码和固定说明。
# 结果与约束：不包含内部路径、数据库正文或其他调用者的信息。
class ManagementTaskError(ValueError):
    # 职责：保存公开错误码和提示。
    # 输入：code、message 由应用控制。
    # 结果与约束：供 DirectRpcError 使用，不转发底层异常正文。
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


TASK_COLUMNS = ('local_aic','peer_aic','group_id','session_key','session_id','task_id','command_id','document','deadline')
TASK_FIELDS = ','.join(TASK_COLUMNS)


def initialize_system_tasks(db):
    db.execute("""CREATE TABLE IF NOT EXISTS system_management_tasks(
        local_aic TEXT NOT NULL, peer_aic TEXT NOT NULL, group_id TEXT NOT NULL,
        session_key TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
        command_id TEXT NOT NULL, document TEXT NOT NULL, deadline DOUBLE PRECISION,
        PRIMARY KEY(local_aic,peer_aic,group_id,session_key,task_id),
        UNIQUE(local_aic,peer_aic,group_id,session_key,command_id))""")


class SystemTaskStore:
    """管理协议回执与业务数据共用 PostgreSQL，权限仍按本体、对端、组和会话隔离。"""
    def __init__(self, database):
        self.database = database

    def initialize(self):
        with self.database.connect(write=True) as db:
            initialize_system_tasks(db)

    # 职责：原子处理已认证管理任务的提交、查询和完成/取消确认。
    # 输入：command 为已验证的 TaskCommand，context 为可信 AipContext，management 为只读业务。
    # 结果与约束：重复 start 返回原结果，跨 peer/session 不可读，确认超时转 failed；短事务失败不报告成功。
    def handle(self, command, context, management):
        operation, task_id = command.get('command'), command.get('taskId')
        if operation not in ('start', 'get', 'complete', 'cancel') or not task_id:
            raise ManagementTaskError(-32602, '仅支持带 taskId 的 start/get/complete/cancel')
        scope = (context.local_aic, context.peer_aic, context.group_id or '')
        session = command.get('sessionId') or ''
        with self.database.connect(write=True) as db:
            rows = db.execute('''SELECT session_key,document,deadline FROM system_management_tasks
                WHERE local_aic=? AND peer_aic=? AND group_id=? AND task_id=? AND (session_key=? OR session_id=?)''',
                              (*scope, task_id, session, session)).fetchall()
            if len(rows) > 1:
                raise ManagementTaskError(-32602, '任务会话不明确，请指定原 sessionId')
            if rows:
                session_key, encoded, deadline = rows[0]
                result = json.loads(encoded)
                if result['status']['state'] == 'awaiting-completion':
                    state = 'failed' if deadline <= time.time() else 'completed' if operation == 'complete' else 'canceled' if operation == 'cancel' else None
                    if state:
                        timestamp = datetime.now(timezone.utc).isoformat()
                        result['status'] = {'state': state, 'stateChangedAt': timestamp}
                        if state == 'failed':
                            result['status']['dataItems'] = [{'type': 'text', 'text': '等待 Leader 确认超时'}]
                        result['id'] = uuid.uuid4().hex
                        result['sentAt'] = timestamp
                        db.execute('''UPDATE system_management_tasks SET document=?,deadline=NULL
                            WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_key=? AND task_id=?''',
                                   (json.dumps(result, ensure_ascii=False), *scope, session_key, task_id))
                return result
            if operation != 'start':
                raise ManagementTaskError(-32001, '当前调用者及会话中没有该任务')
            if db.execute('''SELECT 1 FROM system_management_tasks
                WHERE local_aic=? AND peer_aic=? AND group_id=? AND session_key=? AND command_id=?''',
                          (*scope, session, command['id'])).fetchone():
                raise ManagementTaskError(-32602, '命令 id 已用于其他任务，请沿用原 taskId 重试')
            products, rejection = [], None
            try:
                data = management.execute(management.parse(command.get('dataItems')),db=db)
                text = data['text']
                if data.get('links'):
                    text += '\n' + '\n'.join(f'{key}: {value}' for key, value in data['links'].items())
                products = [{'id': uuid.uuid4().hex, 'dataItems': [{'type': 'text', 'text': text}, {'type': 'data', 'data': data}]}]
            except ValueError as exc:
                rejection = str(exc)
            timestamp = datetime.now(timezone.utc).isoformat()
            status = {'state': 'rejected' if rejection else 'awaiting-completion', 'stateChangedAt': timestamp}
            if rejection:
                status['dataItems'] = [{'type': 'text', 'text': rejection}]
            result = {'type': 'task-result', 'id': uuid.uuid4().hex, 'sentAt': timestamp,
                      'senderRole': 'partner', 'senderId': context.local_aic, 'mentions': [context.peer_aic],
                      'taskId': task_id, 'sessionId': session or uuid.uuid4().hex, 'status': status, 'products': products}
            if context.group_id:
                result['groupId'] = context.group_id
            db.execute('INSERT INTO system_management_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                       (*scope, session, result['sessionId'], task_id, command['id'], json.dumps(result, ensure_ascii=False),
                        None if rejection else time.time() + 300))
            return result
