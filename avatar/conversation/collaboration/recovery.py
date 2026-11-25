"""Schedule peer turns and reconcile interrupted protocol work."""
import asyncio
from avatar.core.dispatch import begin_work, wait_work
import json
import logging
import time
from pathlib import Path
from contextlib import ExitStack
from fastapi import HTTPException
from avatar.core.db import now, dump
from avatar.conversation.loop.peer import PeerTurn, speaker as turn_speaker
from avatar.conversation.loop.store import RunStore
from avatar.conversation.collaboration.results import reply_status
from avatar.core.execution import run_blocking


logger = logging.getLogger(__name__)
RETRY_WINDOW=300
RETRY_TIMEOUT='通信入口自动重试已超时，本请求已停止自动尝试；可重新编辑并发送'


class PeerRecovery:
    def __init__(self, executor, calls):
        self.executor = executor
        self.database = executor.database
        self.resources = executor.resources
        self.transport = executor.transport
        self.close_record = executor.close_record
        self.calls = calls

    # 只有双方均清理的原文才物理回收；个人记忆清理绝不触及另一方保留记录。
    def cleanup(self):
        self.resources.cleanup()
        with ExitStack() as resources, self.database.connect(write=True) as db:
            RunStore.prune_in(db)
            # 修复旧版本已输出明确结束状态却被继续排队的任务，不重放模型。
            stale=db.execute('''SELECT d.id,m.content,m.id AS message_id FROM agent_dialogues d JOIN agent_dialogue_messages m ON m.id=(SELECT MAX(id) FROM agent_dialogue_messages WHERE dialogue_id=d.id) WHERE d.delivery_mode!='message' AND d.status IN ('queued','paused','awaiting_identity') AND m.speaker_role='agent' AND m.task_action='' AND NOT EXISTS(SELECT 1 FROM agent_pending_inputs WHERE task_id=d.id AND applied=0)''').fetchall()
            for row in stale:
                _,state,reason,_=reply_status(row['content'],0)
                if state=='ended':
                    db.execute("UPDATE agent_dialogue_messages SET state='ended',state_detail=? WHERE id=?",(reason,row['message_id']))
                    self.close_record(db,row['id'],reason or '分身已声明任务完成')
            # 未发送的独立任务草稿七天后回收；已创建任务的工作区不在此删除。
            stale=db.execute("SELECT c.id,c.user_id FROM chat_conversations c JOIN agent_intents i ON i.conversation_id=c.id WHERE i.task_id IS NULL AND json_extract(i.document,'$.origin')='peer_task_draft' AND julianday(i.created_at)<julianday('now','-7 days') AND NOT EXISTS(SELECT 1 FROM agent_task_members WHERE resource_conversation_id=c.id) AND NOT EXISTS(SELECT 1 FROM agent_session_members WHERE resource_conversation_id=c.id)").fetchall()
            self.resources.release_workspaces(db,resources,[(row['user_id'],row['id']) for row in stale])
            # 已保存到气泡的调用状态须保留，否则刷新后会丢失取消/确认结果。
            db.execute("DELETE FROM agent_intents WHERE julianday(created_at)<julianday('now','-1 day') AND message_id IS NULL AND status='pending'")
            db.execute('''DELETE FROM agent_dialogue_messages WHERE id IN (SELECT m.id FROM agent_dialogue_messages m JOIN agent_dialogues d ON d.id=m.dialogue_id
              WHERE (d.delivery_mode='message' AND m.id <= (SELECT MIN(cleared_through) FROM agent_session_members WHERE session_id=d.session_id))
              OR (d.status='ended' AND 2=(SELECT COUNT(*) FROM agent_task_members WHERE task_id=d.id AND hidden=1)))''')

    def claim(self):
        with self.database.connect(write=True) as db:
            # 旧等待记录首次调度时建立截止时间；重启或轮询不会延长它。
            db.execute("UPDATE agent_dialogues SET retry_until=? WHERE status='awaiting_identity' AND retry_until=0",(time.time()+RETRY_WINDOW,))
            db.execute("UPDATE agent_dialogues SET status='failed',active_speaker='',error=?,updated_at=? WHERE status='awaiting_identity' AND retry_until<=?",(RETRY_TIMEOUT,now(),time.time()))
            row=db.execute("""SELECT d.* FROM agent_dialogues d JOIN agent_peer_sessions s ON s.id=d.session_id
              WHERE (d.status='queued' OR (d.status='awaiting_identity' AND julianday(d.updated_at)<=julianday('now','-30 seconds'))) AND (d.accepted=1 OR d.delivery_mode='message') AND NOT EXISTS(SELECT 1 FROM agent_dialogues other WHERE other.session_id=d.session_id AND other.status='running')
              ORDER BY d.updated_at,d.created_at,d.id LIMIT 1""").fetchone()
            if not row:return None
            if row['id'] in self.calls:return None
            db.execute("UPDATE agent_dialogues SET status='running',updated_at=? WHERE id=?",(now(),row['id']))
            return dict(row)

    async def once(self):
        job=await run_blocking(self.claim)
        if not job:return False
        task=asyncio.create_task(self.executor.turn(job));self.calls[job['id']]=task
        try:await task
        except (asyncio.CancelledError,Exception) as exc:
            if isinstance(exc,asyncio.CancelledError) and asyncio.current_task().cancelling():
                raise
            from avatar.conversation.collaboration.transport import PeerEndpointUnavailable
            import httpx
            import logging
            import traceback
            frames=[(Path(frame.filename).name,frame.lineno,frame.name) for frame in traceback.extract_tb(exc.__traceback__)]
            logging.getLogger(__name__).warning('Peer invocation failed: task=%s type=%s frames=%s',job['id'],type(exc).__name__,frames)
            deadline=job['retry_until'];error=str(exc.detail) if isinstance(exc,HTTPException) else 'ACPS 调用未完成，未自动重放；请核对证书、对端状态及任务结果'
            if isinstance(exc,ConnectionResetError):error='ACPS TLS 连接被对端重置，可能是对端证书校验暂不可用；未自动重放，请核对对端状态及任务结果'
            elif isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code==429:error='证书平台状态校验限流（HTTP 429）；未自动重放，请等待证书服务恢复后核对任务结果'
            if isinstance(exc,asyncio.CancelledError):status='paused';deadline=0
            elif isinstance(exc,PeerEndpointUnavailable):
                deadline=deadline or time.time()+RETRY_WINDOW
                status='awaiting_identity' if deadline>time.time() else 'failed'
                if status=='failed':error=RETRY_TIMEOUT
            else:
                status='failed'
                deadline=time.time() if isinstance(exc,(TimeoutError,httpx.TimeoutException)) or isinstance(exc,HTTPException) and exc.status_code==504 else 0
                if deadline:error+='；本次调用已超时，未自动重放，可重新编辑并发送'
            with self.database.connect(write=True) as db:
                db.execute("UPDATE agent_dialogues SET status=?,active_speaker='',error=?,retry_until=?,updated_at=? WHERE id=? AND status='running' AND revision=?",(status,error,deadline,now(),job['id'],job['revision']))
        finally:self.calls.pop(job['id'],None)
        return True

    # 完成/取消确认独立持久重试；仅重发幂等 ACPS 控制命令，不重新运行模型。
    async def flush_controls(self,identifier=None):
        with self.database.connect(write=True) as db:
            row=db.execute("SELECT c.*,d.* FROM agent_protocol_controls c JOIN agent_dialogues d ON d.id=c.task_id WHERE c.status='queued' AND c.next_attempt<=?"+(' AND c.task_id=?' if identifier else '')+' LIMIT 1',(time.time(),identifier) if identifier else (time.time(),)).fetchone()
            if not row:return False
            db.execute("UPDATE agent_protocol_controls SET status='running' WHERE task_id=?",(row['task_id'],))
        try:
            transport = self.transport()
            if transport is None:raise HTTPException(503,'ACPS 入口未就绪')
            method=getattr(transport,row['action'])
            await method(dict(row),row['user_id'],row['target_user_id'])
            with self.database.connect(write=True) as db:db.execute("UPDATE agent_protocol_controls SET status='done',error='' WHERE task_id=?",(row['task_id'],))
        except BaseException as error:
            with self.database.connect(write=True) as db:db.execute("UPDATE agent_protocol_controls SET status='queued',next_attempt=?,error=? WHERE task_id=?",(time.time()+30,type(error).__name__,row['task_id']))
            if isinstance(error,asyncio.CancelledError):raise
        return True

    async def control_loop(self):
        while True:
            latch = begin_work(self.database, 'controls')
            if await self.flush_controls():
                await asyncio.sleep(1.)
            else:
                await wait_work(latch, 1., reconcile=2.)

    async def worker(self):
        while True:
            latch = begin_work(self.database, 'peer')
            if not await self.once():
                await wait_work(latch, .4)

    async def cleanup_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.cleanup)
            except Exception as exc:
                logger.error('Peer cleanup failed: type=%s sqlstate=%s',type(exc).__name__,getattr(exc,'sqlstate',None))
            await asyncio.sleep(60)

    def recover(self):
        with self.database.connect(write=True) as db:
            for raw in db.execute("SELECT * FROM agent_dialogues WHERE status='running'").fetchall():
                task = dict(raw)
                safe = PeerTurn.recoverable(self.executor,db,task)
                checkpoint = json.loads(task['agent_checkpoint'] or '{}')
                if task['transport_mode']=='acps':
                    from avatar.conversation.loop.peer import wire_id
                    command=wire_id(db,task,turn_speaker(task))
                    checkpoint.update(speaker_id=turn_speaker(task))
                    checkpoint['resume_wire' if safe else 'unconfirmed_wire']=command
                elif safe:
                    checkpoint.update(speaker_id=turn_speaker(task),resume_internal=True)
                db.execute("UPDATE agent_dialogues SET status=?,agent_checkpoint=?,active_speaker='',error=? WHERE id=?",
                           ('queued' if safe else 'paused',dump(checkpoint) if checkpoint else None,
                            '' if safe else '上次进程中断，未自动重放，请核对后继续',task['id']))
            db.execute("UPDATE agent_protocol_controls SET status='queued' WHERE status='running'")

    async def run(self):
        await run_blocking(self.recover)
        tasks=[asyncio.create_task(self.worker()) for _ in range(4)]+[asyncio.create_task(self.cleanup_loop()),asyncio.create_task(self.control_loop())]
        try:await asyncio.gather(*tasks)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
