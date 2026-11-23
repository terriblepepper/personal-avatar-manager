"""分身传输选择与 WIT Direct 适配：ACPS 优先，未就绪时使用明确授权的同平台内部通道。"""
import uuid
import json
import asyncio
import httpx
from avatar.protocols.transport.directory import CertificateStatusRateLimited
from fastapi import HTTPException
from wit.acps.aip.base import AipContext
from wit.acps.aip.direct import DIRECT_LEADER
from wit.acps.identity import use_local_entity
from avatar.core.db import now
from avatar.core.execution import AgentNeedsInput
from avatar.protocols.transport.endpoints import entity_oidc_identity


class PeerEndpointUnavailable(HTTPException):
    """仅发送前检查可抛出；尚未调用对端，允许等待入口恢复。"""
    def __init__(self,detail):super().__init__(503,detail)


def current_protocol(own_aic,target_aic,*,ready=False):
    return 'acps' if ready or all(not aic.startswith('DEMO-') for aic in (own_aic,target_aic)) else 'internal'


# endpoint 只来自已启动、核验 ACS 的运行时，不接受模型或用户指定地址。
class PeerAcpsTransport:
    def __init__(self,runtime):self.runtime=runtime

    def ready(self,left,right):
        """只检查真实共享入口是否已装载双方身份；不以 Demo AIC 或磁盘文件冒充上线。"""
        return all(uid in self.runtime.instances and self.runtime.status(uid)['serving'] for uid in (left,right))

    async def send(self,job,leader_id,partner_id,operation,command_id):
        local=self.runtime.instances.get(leader_id);remote=self.runtime.instances.get(partner_id)
        if not local or not remote or not self.runtime.status(leader_id)['serving'] or not self.runtime.status(partner_id)['serving']:
            raise PeerEndpointUnavailable('分身 ACPS 证书或共享入口尚未就绪，尚未发出本轮调用')
        try:
            entity=await self.runtime.directories[leader_id].select(local[0])
        except CertificateStatusRateLimited:
            raise PeerEndpointUnavailable('证书平台状态校验限流，正在等待恢复；本轮尚未发送') from None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code==429:
                raise PeerEndpointUnavailable('证书平台状态校验限流（HTTP 429），正在等待恢复；本轮尚未发送') from None
            raise

        command={'type':'task-command','id':command_id,'sentAt':now(),'senderRole':'leader','senderId':local[0],
                 'mentions':[remote[0]],'command':operation,'taskId':job['id'],'sessionId':job['session_id'],
                 'dataItems':[{'type':'data','data':{'peerDialogueTask':job['id']}}] if operation in ('start','continue') else []}
        owner=entity_oidc_identity(self.runtime.settings,leader_id)
        command['commandParams']={'requester':{'id':owner['keycloakSubject'],'issuer':owner['keycloakIssuer']}}
        context=AipContext(local_aic=local[0],peer_aic=remote[0])
        with use_local_entity(entity):
            method=getattr(local[1].context.get(DIRECT_LEADER),{'start':'start_task','continue':'continue_task','complete':'complete_task','cancel':'cancel_task','get':'get_task'}[operation])
            return await method(command,context=context,endpoint=remote[3])

    async def exchange(self,job,leader_id,partner_id):
        with self.runtime.database.connect() as db:
            from avatar.conversation.loop.peer import wire_id
            command_id=wire_id(db,job,partner_id)
            first = db.execute('SELECT command_id FROM agent_wire_results WHERE task_id=? AND session_id=? '
                               'AND local_aic=(SELECT aic FROM avatar_records WHERE user_id=?) '
                               'AND peer_aic=(SELECT aic FROM avatar_records WHERE user_id=?) ORDER BY rowid LIMIT 1',
                               (job['id'], job['session_id'], partner_id, leader_id)).fetchone()
        operation = 'start' if not first or first[0] == command_id else 'continue'
        checkpoint=json.loads(job.get('agent_checkpoint') or '{}')
        if checkpoint.get('resume_wire') == command_id:
            result=await self.send(job,leader_id,partner_id,'get',uuid.uuid4().hex)
        else:
            result=await self.send(job,leader_id,partner_id,operation,command_id)
        try:
            async with asyncio.timeout(480):
                while result.get('status',{}).get('state')=='working':
                    await asyncio.sleep(1)
                    result=await self.send(job,leader_id,partner_id,'get',uuid.uuid4().hex)
        except TimeoutError:
            raise HTTPException(504,'ACPS 原任务结果未确认；不会自动重发，请稍后查询会话') from None
        if result.get('status',{}).get('state')=='awaiting-input':
            items=result['status'].get('dataItems',[])
            attempted=any(i.get('type')=='data' and isinstance(i.get('data'),dict) and i['data'].get('sandbox_attempted') is True for i in items)
            reason=next((item['text'] for item in items if item.get('type')=='text' and isinstance(item.get('text'),str)), '')
            return {'needs_input':reason or '等待当前分身的所有人处理','checkpoint':{'v':1,'sandbox_attempted':attempted}}
        if result.get('status',{}).get('state')!='awaiting-completion':
            items=result.get('status',{}).get('dataItems',[])+[i for p in result.get('products',[]) for i in p.get('dataItems',[])]
            detail='；'.join(i['text'] for i in items if i.get('type')=='text' and isinstance(i.get('text'),str))[:1000] if result.get('status',{}).get('state')=='failed' else ''
            timed_out=result.get('status',{}).get('state')=='failed' and any(i.get('type')=='data' and isinstance(i.get('data'),dict) and i['data'].get('peerReplyTimedOut') is True for i in items)
            raise HTTPException(504 if timed_out else 502,'ACPS 对方回复失败：'+detail if detail else 'ACPS 对方任务未返回完整发言；请核对任务状态后再继续')
        data=[i for p in result.get('products',[]) for i in p.get('dataItems',[])]
        return {'text':'\n'.join(i['text'] for i in data if i.get('type')=='text'),
                'usage':next((i['data'].get('usage') for i in data if i.get('type')=='data' and isinstance(i.get('data'),dict)),None),
                'task_action':next((i['data']['peerTaskAction'] for i in data if i.get('type')=='data' and isinstance(i.get('data'),dict) and 'peerTaskAction' in i['data']),None)}

    async def _control(self,job,left,right,operation):
        with self.runtime.database.connect() as db:
            rows = db.execute('SELECT DISTINCT ON (local_aic,peer_aic) local_aic,peer_aic,state FROM agent_wire_results '
                              'WHERE task_id=? AND session_id=? ORDER BY local_aic,peer_aic,rowid DESC',
                              (job['id'],job['session_id'])).fetchall()
        started = {(r['local_aic'], r['peer_aic']):r['state'] for r in rows}
        directions = {
            (job['target_aic'],job['own_aic']): (left,right),
            (job['own_aic'],job['target_aic']): (right,left),
        }
        if started.keys() - directions.keys():
            raise HTTPException(502,'已受理命令的身份与任务配对不一致，请核对协议记录')
        expected = 'completed' if operation == 'complete' else 'canceled'
        for pair,(leader,partner) in directions.items():
            if pair not in started or (operation=='complete' and started[pair] in ('failed','canceled')):
                continue
            command_id = str(uuid.uuid5(uuid.NAMESPACE_URL,job['id']+':'+operation+':'+leader))
            result = await self.send(job,leader,partner,operation,command_id)
            if (result.get('status',{}).get('state') != expected or
                result.get('taskId') != job['id'] or result.get('sessionId') != job['session_id']):
                raise HTTPException(502,'对端尚未确认当前任务的终态')

    async def complete(self,job,left,right):
        await self._control(job,left,right,'complete')

    async def cancel(self,job,left,right):
        await self._control(job,left,right,'cancel')


class PeerTransport:
    """同平台通信：发送前选择 ACPS 或已授权的内部通道，发送失败后绝不自动换通道重发。"""
    def __init__(self,runtime,dialogues):
        self.acps=PeerAcpsTransport(runtime)
        self.dialogues,self.database=dialogues,dialogues.database

    async def exchange(self,job,leader_id,partner_id):
        """内部调用仅接受库中已授权任务、固定双方及当前发言方，继续复用各方资料和额度隔离。"""
        checkpoint=json.loads(job.get('agent_checkpoint') or '{}')
        if checkpoint.get('unconfirmed_wire'):
            raise HTTPException(409,'原协议命令没有确认结果，请核对对端记录后结束当前任务')
        mode='acps' if self.acps.ready(leader_id,partner_id) else 'internal'
        if checkpoint.get('resume_wire') and mode!='acps':
            raise PeerEndpointUnavailable('正在等待原 ACPS 入口确认已接收命令')
        if mode=='internal' and job.get('transport_mode')=='acps' and job.get('status')=='awaiting_identity':
            raise PeerEndpointUnavailable('ACPS 入口尚未恢复，继续等待原通道，不改走内部传输')
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT * FROM agent_dialogues WHERE id=? AND session_id=?',(job['id'],job['session_id'])).fetchone()
            pair=db.execute('SELECT * FROM agent_peer_sessions WHERE id=?',(job['session_id'],)).fetchone()
            if not row or not pair or row['status']!='running' or row['revision']!=job['revision']:
                raise HTTPException(409,'分身任务状态已变化，本次未发送')
            own,target=self.dialogues.participants(db,row['user_id'],row['target_aic'])
            mode='internal' if checkpoint.get('resume_internal') else current_protocol(own['aic'],target['aic'],ready=mode=='acps')
            speaker=row['user_id'] if row['next_speaker']==0 else row['target_user_id']
            if {leader_id,partner_id}!={own['user_id'],target['user_id']} or partner_id!=speaker or {pair['left_user_id'],pair['right_user_id']}!={leader_id,partner_id}:
                raise HTTPException(403,'分身通信参与者不匹配')
            if row['delivery_mode']!='message' and not row['accepted']:
                raise HTTPException(409,'协商需等待目标本人同意')
            db.execute("UPDATE agent_dialogues SET transport_mode=?,error='' WHERE id=?",(mode,job['id']))
            current=dict(row)
        if mode=='acps':
            result=await self.acps.exchange(job,leader_id,partner_id)
        else:
            try:
                answer=await self.dialogues.answer_peer(current,partner_id)
                result={'text':answer.text,'usage':answer.usage,'task_action':answer.task_action}
            except AgentNeedsInput as pause:
                result={'needs_input':str(pause),'checkpoint':pause.checkpoint}
        return {**result,'transport_mode':mode}

    async def complete(self,job,left,right):
        """仅有真实协议任务时发送完成命令，纯内部消息不等待证书控制命令。"""
        await self.acps.complete(job,left,right)

    async def cancel(self,job,left,right):
        """保留已发起 ACPS 任务的撤销流程，不将内部任务伪报为 ACPS 任务。"""
        await self.acps.cancel(job,left,right)
