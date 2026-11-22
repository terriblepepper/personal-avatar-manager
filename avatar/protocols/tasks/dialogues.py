"""经过真实 mTLS 认证的本平台分身任务；不借用最终用户令牌，不开放任意跨账户调用。"""
import asyncio
import hashlib
import json
import uuid
import httpx
import logging
from fastapi import HTTPException
from avatar.conversation.collaboration.dialogues import confirmed_message
from avatar.conversation.collaboration.tools import task_tool_result
from avatar.conversation.loop.peer import PeerTurn, speaker as turn_speaker, wire_id
from avatar.core.db import now,dump
from avatar.core.execution import AgentNeedsInput
from avatar.protocols.tasks.store import ManagementTaskError
from avatar.protocols.transport.endpoints import entity_oidc_identity


# 仅允许固定配对中的两个实体处理已存在且已接受的任务；普通聊天不要求任务同意。
class PeerDialogueTasks:
    def __init__(self,dialogues,settings=None):
        self.dialogues=dialogues;self.database=dialogues.database;self.running={}
        self.settings=settings or dialogues.transport.acps.runtime.settings

    def scope(self,command,context,user_id,db):
        row=db.execute('SELECT * FROM agent_dialogues WHERE id=? AND session_id=?',(command.get('taskId'),command.get('sessionId'))).fetchone()
        pair=db.execute('SELECT * FROM agent_peer_sessions WHERE id=?',(command.get('sessionId'),)).fetchone()
        peer=db.execute("SELECT a.user_id FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.aic=? AND a.identity_status='active' AND u.account_status='active'",(context.peer_aic,)).fetchone()
        if not row or not pair or not peer or not (row['accepted'] or row['delivery_mode']=='message') or {user_id,peer['user_id']}!={pair['left_user_id'],pair['right_user_id']}:raise ManagementTaskError(-32009,'分身会话或委派任务未授权')
        expected=entity_oidc_identity(self.settings,peer['user_id'],db=db)
        identity=(command.get('commandParams') or {}).get('requester')
        if identity!={'id':expected['keycloakSubject'],'issuer':expected['keycloakIssuer']}:
            raise ManagementTaskError(-32009,'消息中的请求者身份与 mTLS 分身绑定不一致')
        return dict(row)

    def result(self,command,context,state,text='',usage=None,task_action=None):
        if task_action:
            task_action = task_tool_result([task_action])[-1]
        if isinstance(usage, dict):
            usage = {key:value for key,value in usage.items() if key in ('input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens') and type(value) is int}
        else:
            usage = None
        return {'type':'task-result','id':uuid.uuid4().hex,'sentAt':now(),'senderRole':'partner','senderId':context.local_aic,
                'mentions':[context.peer_aic],'taskId':command['taskId'],'sessionId':command['sessionId'],
                'status':{'state':state,'stateChangedAt':now()},'products':[{'id':uuid.uuid4().hex,'dataItems':([{'type':'text','text':text}] if text else [])+[{'type':'data','data':{'usage':usage,**({'peerTaskAction':task_action} if task_action else {})}}]}] if text or task_action else []}

    def validate_delivery(self, db, task, owner):
        receipt = json.loads(task.get('pending_file_delivery') or '{}')
        if receipt.get('authority') and receipt.get('revision')==task['revision'] and receipt.get('speaker')==owner:
            if receipt['authority'] != PeerTurn.source_version(self.dialogues,db,task,owner,receipt['workspace']):
                raise ManagementTaskError(-32009,'原任务资料或工具授权已变化')
            return True
        return False

    @staticmethod
    def latest(db, scope):
        return db.execute(
            'SELECT command_id,state,document FROM agent_wire_results '
            'WHERE local_aic=? AND peer_aic=? AND task_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1', scope
        ).fetchone()

    @staticmethod
    def control(db, operation, latest, key):
        if latest is None:
            raise ManagementTaskError(-32001, '该方向没有已受理的任务')
        result = json.loads(latest['document'])
        target = 'completed' if operation == 'complete' else 'canceled'
        if latest['state'] == target:
            return result
        allowed = ('awaiting-completion',) if operation == 'complete' else ('working','awaiting-completion','awaiting-input')
        if latest['state'] not in allowed:
            raise ManagementTaskError(-32010, '当前任务状态不允许该操作')
        stamp = now()
        result.update(id=uuid.uuid4().hex, sentAt=stamp, status={'state':target,'stateChangedAt':stamp})
        db.execute('UPDATE agent_wire_results SET state=?,document=? WHERE local_aic=? AND peer_aic=? AND command_id=?',
                   (target,dump(result),*key[:2],latest['command_id']))
        return result

    # start/continue 生成一方发言；command id 去重使网络重试不会重复调用模型。
    async def handle(self,command,context,user_id):
        operation = command.get('command')
        if operation not in ('start','continue','get','complete','cancel'):
            raise ManagementTaskError(-32602,'不支持的分身任务命令')
        key = (context.local_aic,context.peer_aic,command['id'])
        scope = (*key[:2],command['taskId'],command['sessionId'])
        if operation == 'get':
            with self.database.connect() as db:
                task = self.scope(command,context,user_id,db)
                latest = self.latest(db,scope)
                self.validate_delivery(db,task,user_id)
                if (latest and latest['state'] == 'working' and scope not in self.running
                        and task.get('status') == 'running' and task.get('id') and turn_speaker(task)==user_id
                        and latest['command_id']==wire_id(db,task,user_id)
                        and PeerTurn.recoverable(self.dialogues,db,task)):
                    self.schedule(task,user_id,command,context,scope,(*key[:2],latest['command_id']))
            if not latest:
                raise ManagementTaskError(-32001,'该方向没有已受理的任务')
            return json.loads(latest['document'])

        semantic = {field: command.get(field) for field in
                    ('command','taskId','sessionId','groupId','commandParams','dataItems','mentions')}
        digest = hashlib.sha256(json.dumps(semantic,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        is_control = operation in ('complete','cancel')
        with self.database.connect(write=True) as db:
            task = self.scope(command,context,user_id,db)
            pending_delivery = not is_control and self.validate_delivery(db,task,user_id)
            receipt = db.execute('SELECT fingerprint,document FROM agent_wire_receipts WHERE local_aic=? AND peer_aic=? AND command_id=?',key).fetchone()
            if receipt:
                if receipt['fingerprint'] != digest:
                    raise ManagementTaskError(-32602,'命令 ID 已用于其他请求')
                if receipt['document'] is not None:
                    return json.loads(receipt['document'])
            prior = db.execute('SELECT * FROM agent_wire_results WHERE local_aic=? AND peer_aic=? AND command_id=?',key).fetchone()
            if prior:
                if receipt is None or is_control or (prior['task_id'],prior['session_id']) != scope[2:]:
                    raise ManagementTaskError(-32602,'命令 ID 已用于其他请求')
                return json.loads(prior['document'])
            latest = self.latest(db,scope)
            if is_control:
                result = self.control(db,operation,latest,key)
            else:
                if pending_delivery:
                    raise ManagementTaskError(-32010,'当前发言已生成，请查询已受理的命令')
                speaker = task['user_id'] if task['next_speaker']==0 else task['target_user_id']
                if speaker != user_id or task['status'] != 'running':
                    raise ManagementTaskError(-32010,'任务当前不等待该分身发言')
                resumable=latest and latest['state']=='awaiting-input' and task.get('agent_checkpoint')
                if resumable:
                    pause=json.loads(latest['document'])['status']['dataItems']
                    resumable=any(i.get('type')=='data' and task['revision']>i['data']['revision'] for i in pause)
                if (operation == 'start') != (latest is None) or latest and latest['state'] != 'awaiting-completion' and not resumable:
                    raise ManagementTaskError(-32010,'该方向当前状态不允许发言')
                text = confirmed_message(task,user_id)
                state = 'awaiting-completion' if text is not None else 'working'
                result = self.result(command,context,state,text or '')
                db.execute('INSERT INTO agent_wire_results VALUES(?,?,?,?,?,?,?)',
                           (command['id'],*key[:2],command['sessionId'],command['taskId'],state,dump(result)))
            db.execute('INSERT INTO agent_wire_receipts(local_aic,peer_aic,command_id,fingerprint,document) VALUES(?,?,?,?,?)',
                       (*key,digest,dump(result) if is_control else None))
        if is_control:
            # 先持久化终态，再取消本进程的调用；迟到的回写必须仍匹配 working。
            if operation == 'cancel' and (active := self.running.get(scope)):
                active.cancel()
            return result
        if result['status']['state']=='awaiting-completion':return result
        self.schedule(task,user_id,command,context,scope,key)
        return result

    def schedule(self,task,user_id,command,context,scope,key):
        task = {**task, '_wire_key':key}
        pending=asyncio.create_task(self.generate(task,user_id,command,context,scope,key))
        self.running[scope]=pending
        def completed(job):
            if self.running.get(scope) is job:self.running.pop(scope,None)
            self.completed(job)
        pending.add_done_callback(completed)

    @staticmethod
    def completed(task):
        if not task.cancelled() and task.exception():
            logging.getLogger(__name__).error('Peer generation cleanup failed',exc_info=task.exception())

    async def stop(self):
        tasks=tuple(self.running.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    def recover(self):
        with self.database.connect(write=True) as db:
            for row in db.execute("SELECT * FROM agent_wire_results WHERE state='working'").fetchall():
                linked = db.execute('SELECT * FROM agent_dialogues WHERE id=? AND session_id=?',(row['task_id'],row['session_id'])).fetchone()
                if linked:
                    task = dict(linked)
                    if (task['status'] in ('running','queued') and row['command_id']==wire_id(db,task,turn_speaker(task))
                            and PeerTurn.recoverable(self.dialogues,db,task)):
                        continue
                result=json.loads(row['document'])
                result.update(id=uuid.uuid4().hex,sentAt=now())
                result['status']={'state':'failed','stateChangedAt':now(),'dataItems':[{'type':'text','text':'服务重启，原请求结果未确认；不会自动重发'}]}
                db.execute("UPDATE agent_wire_results SET state='failed',document=? WHERE local_aic=? AND peer_aic=? AND command_id=? AND state='working'",(dump(result),row['local_aic'],row['peer_aic'],row['command_id']))

    async def generate(self,task,user_id,command,context,scope,key):
        try:
            answer=await self.dialogues.answer_peer(task,user_id)
            result=self.result(command,context,'awaiting-completion',answer.text,answer.usage,answer.task_action)
        except AgentNeedsInput as pause:
            result=self.result(command,context,'awaiting-input')
            result['status']['dataItems']=[{'type':'text','text':pause.question or str(pause)}, {'type':'data','data':{
                'revision':task['revision'], 'sandbox_attempted':pause.checkpoint['sandbox_attempted']}}]
        except asyncio.CancelledError:
            raise
        except HTTPException as error:
            result=self.result(command,context,'failed','分身任务或资料状态已变化，请核对后继续' if error.status_code in (403,404,409) else '接收方未完成发言，请核对任务状态')
            if error.status_code==504:result['status']['dataItems']=[{'type':'data','data':{'peerReplyTimedOut':True}}]
        except (TimeoutError,httpx.TimeoutException):
            result=self.result(command,context,'failed','接收方模型调用已超时，未自动重新调用')
            result['status']['dataItems']=[{'type':'data','data':{'peerReplyTimedOut':True}}]
        except Exception:
            result=self.result(command,context,'failed','接收方生成失败，请检查模型配置；未自动重新调用')
        finally:
            if self.running.get(scope) is asyncio.current_task():self.running.pop(scope,None)
        with self.database.connect(write=True) as db:
            if result['status']['state'] in ('awaiting-completion','awaiting-input') and task.get('id'):
                try:
                    current=self.dialogues.owned(db,{'id':user_id},task['id'])
                    if current['revision']!=task['revision'] or current['status']!='running' or turn_speaker(current)!=user_id:
                        raise HTTPException(409, '任务状态已改变')
                    if run := task.get('_peer_run'):
                        run.validate(db)
                except HTTPException:
                    result=self.result(command,context,'failed','原任务已停止或资料授权已变化')
            if not db.execute("UPDATE agent_wire_results SET state=?,document=? WHERE local_aic=? AND peer_aic=? AND command_id=? AND state='working'",(result['status']['state'],dump(result),*key)).rowcount:
                stored = db.execute('SELECT document FROM agent_wire_results WHERE local_aic=? AND peer_aic=? AND command_id=?',key).fetchone()
                if stored: return json.loads(stored[0])
        return result
