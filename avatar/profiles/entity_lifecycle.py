# 模块说明：管理员管理个人正式身份的持久队列；Demo 迁移与日常资料审核相互独立。
import asyncio

from avatar.core.dispatch import begin_work, wait_work
import json
import logging
import time
import uuid
import httpx
from datetime import datetime
from fastapi import HTTPException
from wit.acps.identity.acs import AcsContractError
from avatar.core.db import now, dump, event
from avatar.protocols.registration.entity import EntityRegistrationRunner, staged_entity_metadata
from avatar.protocols.registration.maintenance import RENEWAL_SCAN_SECONDS, maintain_entity, remove_retired_identity
from avatar.protocols.registration.base import RegistrationPending, RegistrationFailure
from avatar.protocols.identity.errors import identity_error_detail

def remap_identity(db,user_id,old,new):
    """原件、聊天及 Soul 仍归属稳定 user_id，仅迁移协议寻址。"""
    db.execute('UPDATE chat_conversations SET target_aic=? WHERE target_aic=?', (new, old))
    db.execute('UPDATE chat_conversations SET selected_aic=? WHERE selected_aic=?', (new, old))
    db.execute('UPDATE chat_messages SET responder_aic=? WHERE responder_aic=?', (new, old))
    db.execute('UPDATE agent_dialogues SET own_aic=? WHERE user_id=?',(new,user_id))
    db.execute('UPDATE agent_dialogues SET target_aic=? WHERE target_user_id=?',(new,user_id))
    db.execute('UPDATE agent_dialogue_messages SET speaker_aic=? WHERE speaker_aic=?',(new,old))
    db.execute('INSERT INTO avatar_identity_changes VALUES(?,?,?,?)', (old, new, user_id, now()))


log = logging.getLogger(__name__)


# 职责：管理个人派生注册、注销、正式证书续期的可恢复任务。
# 输入：数据库、服务器配置，可注入真实契约兼容的测试 runner/maintainer。
# 结果与约束：远程操作不持数据库写锁；只有完整 active 才替换 Demo AIC，正常审核不得悄悄更换身份。
class EntityLifecycleService:
    # 职责：装配队列依赖与注册适配器。
    # 输入：database、settings；runner/maintainer 只供隔离验证注入。
    # 结果与约束：构造不联网，不自动批量迁移。
    def __init__(self, database, settings, runner=None, maintainer=None):
        self.database, self.settings = database, settings
        if runner is None and settings.provider=='acps-direct':
            from avatar.protocols.registration.direct import DirectRegistrationRunner
            runner=DirectRegistrationRunner(settings)
        self.runner = runner or EntityRegistrationRunner(settings)
        self.maintainer = maintainer or maintain_entity

    # 职责：校验目标并排入一个身份维护任务。
    # 输入：admin 为已认证管理员，user_id 为既有分身账户，action 为 register/renew/unregister。
    # 结果与约束：同账户仅一项活动操作；注销立即关闭本地协议接入，注册失败不影响原 Demo 资料与问答。
    def enqueue(self, admin, user_id, action):
        if action not in ('register', 'renew', 'unregister'):
            raise HTTPException(422, '不支持的身份维护操作')
        if action=='register' and self.settings.provider=='acps-direct':
            raise HTTPException(409,'独立实体须由用户提交最新资料及 ACS，经平台审核后进入 Registry 审核')
        if not self.settings.entity_aip_enabled:
            raise HTTPException(409, '请先配置并启用个人分身协议端点')
        with self.database.connect(write=True) as db:
            row = db.execute('SELECT a.*,u.account_status FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.user_id=?', (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, '分身不存在')
            current = db.execute("SELECT * FROM avatar_identity_jobs WHERE user_id=? AND status IN ('queued','running','waiting')", (user_id,)).fetchone()
            if current:
                if current['action'] == action:
                    return dict(current)
                raise HTTPException(409, '该分身正在执行其他身份维护操作')
            if db.execute("SELECT 1 FROM provision_jobs j JOIN profile_submissions s ON s.id=j.submission_id WHERE s.user_id=? AND j.status IN ('queued','running','waiting')", (user_id,)).fetchone():
                raise HTTPException(409, '资料同步任务尚未完成，请稍后维护身份')
            if action == 'unregister' and row['identity_status'] == 'retired':
                raise HTTPException(409, '该身份已注销')
            demo = row['aic'].startswith('DEMO-')
            remote_aic = row['aic'] if not demo else (staged_entity_metadata(self.settings.data_dir, user_id) or {}).get('aic', '')
            if action == 'register':
                if not demo or row['account_status'] != 'active' or row['identity_status'] == 'unregistered':
                    raise HTTPException(409, '仅可将已激活账户的 Demo 分身升级为正式派生身份')
                if db.execute("SELECT 1 FROM chat_conversations WHERE (target_aic=? OR selected_aic=?) AND lease_until>?", (row['aic'], row['aic'], time.time())).fetchone():
                    raise HTTPException(409, '分身正在回答，请结束当前对话后再迁移')
            elif demo and (action != 'unregister' or not remote_aic):
                raise HTTPException(409, '请先完成正式派生注册')
            elif row['identity_status'] != 'active' and action != 'unregister':
                raise HTTPException(409, '分身已经停用或正在注销，不能更新证书')
            identifier, timestamp = str(uuid.uuid4()), now()
            db.execute('INSERT INTO avatar_identity_jobs(id,user_id,actor,action,expected_aic,status,created_at,updated_at,remote_aic) VALUES(?,?,?,?,?,?,?,?,?)',
                       (identifier, user_id, admin['username'], action, row['aic'], 'queued', timestamp, timestamp, remote_aic or ''))
            if action == 'unregister':
                db.execute("UPDATE avatar_records SET identity_status='unregistering' WHERE user_id=?", (user_id,))
            event(db, user_id, admin['username'], '提交个人身份维护任务', action, actor_id=admin['id'])
            return dict(db.execute('SELECT * FROM avatar_identity_jobs WHERE id=?', (identifier,)).fetchone())

    def schedule_renewals(self):
        if not self.settings.entity_aip_enabled:
            return
        timestamp = now()
        with self.database.connect(write=True) as db:
            due = db.execute("""SELECT a.user_id,a.aic,json_extract(a.certificate,'$.id') AS fingerprint
                FROM avatar_records a JOIN users u ON u.id=a.user_id
                WHERE a.identity_status='active' AND u.account_status='active' AND a.aic NOT LIKE 'DEMO-%'
                AND julianday(json_extract(a.certificate,'$.expires_at')) <= julianday(?, '+1 day')
                AND NOT EXISTS (SELECT 1 FROM avatar_identity_jobs j WHERE j.user_id=a.user_id
                    AND (j.status IN ('queued','waiting','running') OR (j.action='renew' AND j.status='failed'
                         AND j.expected_aic=a.aic AND j.updated_at>=a.updated_at)))
                AND NOT EXISTS (SELECT 1 FROM provision_jobs j JOIN profile_submissions s ON s.id=j.submission_id
                    WHERE s.user_id=a.user_id AND j.status IN ('queued','waiting','running'))""",(timestamp,)).fetchall()
            for row in due:
                if not row['fingerprint']:
                    continue
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'avatar-renew:'+row['aic']+':'+row['fingerprint']))
                inserted = db.execute("""INSERT INTO avatar_identity_jobs
                    (id,user_id,actor,action,expected_aic,status,created_at,updated_at,remote_aic)
                    VALUES(?,?,'system','renew',?,'queued',?,?,?) ON CONFLICT DO NOTHING""",
                    (job_id,row['user_id'],row['aic'],timestamp,timestamp,row['aic']))
                if inserted.rowcount:
                    event(db,row['user_id'],'system','证书到期自动续签已排队',job_id)

    def refresh(self, admin, identifier):
        with self.database.connect(write=True) as db:
            job=db.execute('SELECT * FROM avatar_identity_jobs WHERE id=?',(identifier,)).fetchone()
            if not job:raise HTTPException(404,'身份维护任务不存在')
            if job['status']!='waiting':raise HTTPException(409,'只能刷新等待中的身份维护任务')
            db.execute("UPDATE avatar_identity_jobs SET status='queued',next_attempt_at=0,updated_at=? WHERE id=?",(now(),identifier))
            event(db,job['user_id'],admin['username'],'手动刷新身份审核与证书状态',identifier,actor_id=admin['id'])
        return {'status':'queued'}

    # 职责：执行队列中的一项个人身份维护操作。
    # 输入：从服务器持久队列认领最早的到期任务。
    # 结果与约束：并发更改、活跃会话及 AIC 不一致拒绝提交；失败保存固定提示，不写远端正文或私钥。
    async def once(self):
        with self.database.connect(write=True) as db:
            job = db.execute("SELECT * FROM avatar_identity_jobs WHERE status='queued' OR (status='waiting' AND next_attempt_at<=?) ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if not job:
                return False
            db.execute("UPDATE avatar_identity_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), job['id']))
            row = db.execute('SELECT * FROM avatar_records WHERE user_id=?', (job['user_id'],)).fetchone()
        cleanup = False
        try:
            if not row or row['aic'] != job['expected_aic']:
                raise RegistrationFailure('账户分身身份已经变化，请刷新核对')
            if job['action'] == 'renew' and job['actor'] == 'system' and uuid.UUID(job['id']).version == 5:
                fingerprint = json.loads(row['certificate']).get('id','')
                expected = str(uuid.uuid5(uuid.NAMESPACE_URL, 'avatar-renew:'+row['aic']+':'+fingerprint))
                if job['id'] != expected or row['identity_status'] != 'active':
                    raise RegistrationFailure('排队后身份或证书已变化，跳过旧续签任务')
            cleanup = job['action'] == 'unregister' and row['identity_status'] == 'retired'
            if not cleanup:
                async with asyncio.timeout(self.settings.system_atr_call_timeout):
                    if job['action'] == 'register':
                        result = await self.runner.advance(job['user_id'], json.loads(row['profile']))
                    else:
                        result = await self.maintainer(self.settings, job['user_id'], job['remote_aic'] or row['aic'], job['action'])
                cleanup = self.commit(job, row, result)
            if cleanup:
                remove_retired_identity(self.settings.data_dir, job['user_id'])
                with self.database.connect(write=True) as db:
                    self.complete(db, job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            limited = job['action']=='register' and isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code==429
            waiting = cleanup or limited or job['action'] != 'renew' and isinstance(exc, (RegistrationPending, TimeoutError, httpx.RequestError))
            delay = 3600
            message = str(exc) if isinstance(exc, (RegistrationPending, RegistrationFailure)) else '远端身份或证书契约核验失败；保留原 AIC 与 ATR 状态，请管理员对账后重试'
            if cleanup:
                message = '远端身份已注销，等待完成本地身份目录清理'
            if job['action'] == 'renew' and isinstance(exc, (TimeoutError, httpx.RequestError)):
                message = '证书续期响应不确定，保留原状态；先使用 wit status 核对证书代次，再决定是否重试'
            if isinstance(exc, AcsContractError):
                message = identity_error_detail(exc) + '；已保留获分配的 AIC，请平台修复既有 ACS 后重试'
            elif isinstance(exc, httpx.HTTPStatusError):
                message = identity_error_detail(exc) + '；请核对 Registry/CA 状态和权限，原身份及私有状态保留'
            log.warning('个人身份维护未完成：action=%s type=%s', job['action'], type(exc).__name__)
            with self.database.connect(write=True) as db:
                db.execute('UPDATE avatar_identity_jobs SET status=?,error=?,next_attempt_at=?,updated_at=? WHERE id=?',
                    ('waiting' if waiting else 'failed', message, time.time() + delay, now(), job['id']))
                event(db, job['user_id'], job['actor'], '个人身份维护等待' if waiting else '个人身份维护失败', message)
        return True

    # 职责：原子提交已被 wit 核验的迁移或证书维护结果。
    # 输入：job 是认领记录，before 是远端操作前的分身行，result 是正式身份或证书投影。
    # 结果与约束：迁移只改身份和会话目标引用，保留资料、草稿、附件、Key、账户及原消息正文。
    def commit(self, job, before, result):
        with self.database.connect(write=True) as db:
            row = db.execute('SELECT * FROM avatar_records WHERE user_id=?', (job['user_id'],)).fetchone()
            if not row or row['aic'] != before['aic'] or row['updated_at'] != before['updated_at']:
                raise RegistrationFailure('维护期间分身数据已变化，保留远端状态，请重新核对后重试')
            if job['action'] == 'register':
                old, new = before['aic'], result['aic']
                document = result['acs_document']
                if not old.startswith('DEMO-') or new.startswith('DEMO-') or document.get('aic') != new or document.get('entityUserId') != job['user_id']:
                    raise RegistrationFailure('迁移结果身份核验失败')
                if db.execute('SELECT 1 FROM chat_conversations WHERE (target_aic=? OR selected_aic=?) AND lease_until>?', (old, old, time.time())).fetchone():
                    raise RegistrationFailure('迁移期间分身正在回答；保留正式状态，请结束对话后重试')
                db.execute('UPDATE avatar_records SET aic=?,certificate=?,acs_document=?,identity_status=\'active\',updated_at=? WHERE user_id=?',
                           (new, dump(result['certificate']), dump(document), now(), job['user_id']))
                remap_identity(db,job['user_id'],old,new)
            elif job['action'] == 'renew':
                previous = json.loads(before['certificate'])
                if (result.get('id') == previous.get('id') or not result.get('expires_at') or
                        previous.get('expires_at') and datetime.fromisoformat(result['expires_at']) <= datetime.fromisoformat(previous['expires_at'])):
                    raise RegistrationFailure('续签未读回有效期更长的新证书')
                db.execute('UPDATE avatar_records SET certificate=?,updated_at=? WHERE user_id=?', (dump(result), now(), job['user_id']))
            else:
                certificate = json.loads(row['certificate'])
                if not row['aic'].startswith('DEMO-'):
                    certificate['status'] = '已注销并撤销证书'
                if self.settings.provider=='acps-direct':
                    db.execute("UPDATE avatar_records SET identity_status='retired',certificate='{}',acs_document='{}',updated_at=? WHERE user_id=?",(now(),job['user_id']))
                    db.execute("UPDATE profile_records SET status='draft',reason='原实体已注销；请填写新 ACS 后重新提交双层审核',updated_at=? WHERE user_id=?",(now(),job['user_id']))
                    # retired 与活动任务同在时，即使进程中断，恢复也只操作本地目录。
                    return True
                db.execute("UPDATE avatar_records SET identity_status='unregistered',certificate=?,updated_at=? WHERE user_id=?", (dump(certificate), now(), job['user_id']))
            self.complete(db, job)
            return False

    @staticmethod
    def complete(db, job):
        db.execute("UPDATE avatar_identity_jobs SET status='done',error='',updated_at=? WHERE id=?", (now(), job['id']))
        event(db, job['user_id'], job['actor'], '个人身份维护完成', job['action'])

    # 职责：恢复进程中断的维护任务并持续轮询。
    # 输入：由应用单进程生命周期启动。
    # 结果与约束：注册复用原 ATR 状态，注销逐项恢复；已开始的续期中断后要求人工核对，不自动重复签发。
    async def run(self):
        with self.database.connect(write=True) as db:
            db.execute("UPDATE avatar_identity_jobs SET status='queued' WHERE status='running' AND action!='renew'")
            db.execute("UPDATE avatar_identity_jobs SET status='failed',error='续期被中断，请先使用 wit status 核对证书代次，再决定是否重试' WHERE status='running' AND action='renew'")
        next_scan = 0.0
        while True:
            latch = begin_work(self.database, 'identity')
            try:
                if time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + RENEWAL_SCAN_SECONDS
                    await asyncio.to_thread(self.schedule_renewals)
                if not await self.once():
                    await wait_work(latch, 1.)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('个人身份队列异常：%s', type(exc).__name__)
                await asyncio.sleep(2)
