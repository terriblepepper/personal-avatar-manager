# 模块说明：后台创建任务：执行审核通过的持久化队列，单进程轮询 Demo 或正式本体派生注册器。
import asyncio
import logging
import time
from datetime import datetime
from avatar.core.db import dump, event, now
from avatar.profiles.services import AvatarService
from avatar.protocols.identity.acs import resolve_input, build_acs, public_base_url
from avatar.protocols.registration.base import RegistrationPending, RegistrationFailure

log = logging.getLogger(__name__)


# 职责：轮询持久化队列并创建或更新审核通过的分身。
# 输入：每个应用进程启动一个工作协程，当前部署使用单个 uvicorn worker。
# 结果与约束：Demo 注册可幂等重放；真实注册重试须先对账，多进程扩展须增加可靠认领和租约机制。
class ProvisionWorker:
    """One worker per application process; deploy with one uvicorn worker.

    Demo provider is replay-safe. Real remote writes require Registry reconciliation
    instead of blind restart recovery; add leases/outbox for multi-worker deployment.
    """
    # 职责：注入队列存储和分身注册适配器。
    # 输入：database 为 Database，provider 实现 provision 与 renew 契约。
    # 结果与约束：只保存依赖，不启动后台任务。
    def __init__(self, database, provider):
        self.database, self.provider = database, provider

    # 职责：恢复进程异常退出时仍标为运行中的任务。
    # 输入：扫描 jobs 中 running 状态的记录。
    # 结果与约束：将其改为 queued 供重放；Demo 可幂等重放；正式派生使用原 wit 状态恢复，不确定提交由框架阻断并要求对账，不重新创建。
    def recover(self):
        with self.database.connect(write=True) as db:
            db.execute("UPDATE provision_jobs SET status='queued',updated_at=? WHERE status='running'", (now(),))

    # 职责：认领并执行最早的一条审核通过的排队任务。
    # 输入：无参数，从 submissions 读取审核资料及 ACS 候选；模型配置保持独立，传入旧分身保持身份。
    # 结果与约束：无任务返回 False，处理过返回 True；成功事务同步分身与任务，失败记录脱敏原因，远程调用不占数据库写事务。
    async def once(self):
        with self.database.connect(write=True) as db:
            job = db.execute("SELECT j.id AS job_id,j.attempts AS prior_attempts,j.registration_phase,s.* FROM provision_jobs j JOIN profile_submissions s ON s.id=j.submission_id WHERE (j.status='queued' OR (j.status='waiting' AND j.next_attempt_at<=?)) AND s.status='approved' AND NOT EXISTS (SELECT 1 FROM avatar_identity_jobs m WHERE m.user_id=s.user_id AND m.status IN ('queued','running','waiting')) ORDER BY j.updated_at LIMIT 1", (time.time(),)).fetchone()
            if not job:
                return False
            db.execute("UPDATE provision_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), job["job_id"]))
            old = AvatarService.decode_agent(db.execute("SELECT * FROM avatar_records WHERE user_id=?", (job["user_id"],)).fetchone())
        phase = "registry_submitting"
        try:
            if old and datetime.fromisoformat(old['last_modified_at']) > datetime.fromisoformat(job['last_modified_at']):
                with self.database.connect(write=True) as db:
                    db.execute("UPDATE provision_jobs SET status='done',registration_phase='superseded',error='',updated_at=? WHERE id=?", (now(), job['job_id']))
                    event(db, job['user_id'], 'system', '跳过已被新资料替代的旧同步任务', actor_id=job['reviewer'])
                return True
            import json
            profile = json.loads(job["snapshot"])
            profile.pop("attachments",None)
            # 外部创建可能耗时，先结束认领事务再 await，避免长期持有 SQLite 写锁。
            direct=getattr(self.provider,'mode','')=='acps-direct'
            cleanup_pending = job['registration_phase'] == 'identity_cleanup'
            if old and direct and old.get('identity_status') in ('active','unregistering'):
                from avatar.protocols.registration.direct import active_input, direct_candidate, personal_input
                incoming=personal_input(json.loads(job['acs_input']),profile)
                existing=active_input(old)
                reuse = job['reuse_signed_acs']
                if reuse and json.loads(job['acs_document']) != old.get('acs_document'):
                    raise RegistrationFailure('审核快照绑定的正式 ACS 已变化，请核对当前身份')
                replacing = not reuse and incoming != existing
                if replacing or old['identity_status']=='unregistering':
                    direct_candidate(self.provider.runner.settings,job['user_id'],profile,json.loads(job['acs_input']))
                    with self.database.connect(write=True) as db:
                        if db.execute('SELECT 1 FROM chat_conversations WHERE user_id=? AND lease_until>?',(job['user_id'],time.time())).fetchone():
                            raise RegistrationPending('等待当前对话结束后更新 ACS',10,phase='draining')
                        db.execute("UPDATE avatar_records SET identity_status='unregistering' WHERE user_id=?",(job['user_id'],))
                    from avatar.protocols.registration.maintenance import maintain_entity
                    settings=self.provider.runner.settings
                    phase = 'certificate_revocation'
                    await maintain_entity(settings,job['user_id'],old['aic'],'unregister')
                    with self.database.connect(write=True) as db:
                        db.execute("UPDATE avatar_records SET identity_status='retired',certificate='{}',acs_document='{}',updated_at=? WHERE user_id=?",(now(),job['user_id']))
                        db.execute("UPDATE provision_jobs SET registration_phase='identity_cleanup' WHERE id=?", (job['job_id'],))
                    cleanup_pending = True
                    old={**old,'identity_status':'retired','certificate':{},'acs_document':{}}
                elif not old['aic'].startswith('DEMO-'):
                    phase = 'identity_verification'
                    from avatar.protocols.registration.direct import verify_existing
                    await verify_existing(self.provider.runner.settings,job['user_id'],old)
            retired=bool(old and old.get("identity_status")=="retired" and getattr(self.provider,"mode","")=="acps-direct")
            if cleanup_pending:
                if not retired:
                    raise RegistrationFailure('待清理身份与账户当前状态不匹配，请核对')
                from avatar.protocols.registration.maintenance import remove_retired_identity
                remove_retired_identity(self.provider.runner.settings.data_dir, job['user_id'])
                # 新注册可写入同一路径；先持久化清理完成，重启时才不会误删新候选。
                with self.database.connect(write=True) as db:
                    db.execute("UPDATE provision_jobs SET registration_phase='queued' WHERE id=?", (job['job_id'],))
            if direct and old and not retired and old['identity_status'] not in ('active','unregistering'):
                raise RegistrationFailure('原身份未激活，请先核对身份维护记录')
            if retired:phase = 'certificate_issuing'
            identity = await self.provider.provision(job["user_id"], profile, None if retired else old, **({"acs_input":json.loads(job["acs_input"])} if getattr(self.provider,"mode","")=="acps-direct" else {}))
            with self.database.connect(write=True) as db:
                if direct and (not old or retired):
                    doc = identity.get('acs_document') or {}
                    cert = identity.get('certificate') or {}
                    if (doc.get('active') is not True or doc.get('aic') != identity.get('aic')
                            or doc.get('entityUserId') != job['user_id'] or not cert.get('public_pem') or not cert.get('server_public_pem')):
                        raise RegistrationFailure('新注册的 AIC、正式 ACS 或双证书未核验完整',phase='identity_verification')
                if direct:
                    from avatar.protocols.registration.direct import personal_input
                    content = personal_input(json.loads(job['acs_input']), profile)
                else:
                    content = resolve_input(json.loads(job['acs_input']), profile)
                document = identity.get('acs_document') or build_acs(content, job['user_id'], public_base_url(db), identity['aic'])
                # 正式 ACS 契约已绑定时不自动覆盖；新候选继续留在审核快照；派生正式能力继承本体，不能按用户改写注册声明。
                previous_document = old.get('acs_document', {}) if old else {}
                keep_input = old and not retired and (direct or not old.get('acs_input', {}).get('template_owner'))
                if not retired and previous_document.get('aic') and (keep_input or not previous_document['aic'].startswith('DEMO-')):
                    document = previous_document
                if old and not retired and old['aic'] != identity['aic']:
                    raise RegistrationFailure('更新不得替换已有分身 AIC，请使用专门迁移流程')
                saved_input = content.model_dump()
                if keep_input:
                    saved_input = old.get('acs_input', {})
                elif direct:
                    from avatar.protocols.registration.direct import signed_input
                    saved_input = signed_input(saved_input, document)
                    if document.get('skills') and not document['skills'][0]['description'].startswith(content.template_owner+'能够'):
                        saved_input.pop('template_owner', None)
                db.execute("INSERT INTO avatar_records(user_id,aic,certificate,profile,last_modified_at,created_at,updated_at,acs_input,acs_document) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET aic=excluded.aic,identity_status='active',certificate=excluded.certificate,profile=excluded.profile,acs_input=excluded.acs_input,acs_document=excluded.acs_document,last_modified_at=excluded.last_modified_at,updated_at=excluded.updated_at",
                           (job["user_id"], identity["aic"], dump(identity["certificate"]), dump(profile), job["last_modified_at"], now(), now(), dump(saved_input), dump(document)))
                # 先写入该用户当前 AIC，再迁移会话引用；参与者触发器会核验账户归属。
                if retired and old['aic']!=identity['aic']:
                    from avatar.profiles.entity_lifecycle import remap_identity
                    remap_identity(db,job['user_id'],old['aic'],identity['aic'])
                completed_phase = 'certificate_reused' if direct and old and not retired and not old['aic'].startswith('DEMO-') else 'active'
                db.execute("UPDATE provision_jobs SET status='done',registration_phase=?,error='',updated_at=? WHERE id=?", (completed_phase, now(), job["job_id"]))
                db.execute("UPDATE profile_records SET status='active',reason='',updated_at=? WHERE user_id=? AND last_modified_at=? AND status='creating'", (now(), job["user_id"], job['last_modified_at']))
                event(db, job["user_id"], "system", "分身资料已同步" if old else "分身自动注册完成", identity["aic"], actor_id=job["reviewer"])
        except RegistrationPending as exc:
            with self.database.connect(write=True) as db:
                db.execute("UPDATE provision_jobs SET status='waiting',registration_phase=?,error=?,updated_at=?,next_attempt_at=? WHERE id=?",
                           (exc.phase,str(exc), now(), time.time() + (exc.delay if exc.phase=='draining' else 3600), job['job_id']))
                # 只有当前草稿仍是创建中时更新提示，不回退用户刚提交的新草稿或审核状态。
                db.execute("UPDATE profile_records SET reason=? WHERE user_id=? AND status='creating' AND last_modified_at=?", (str(exc), job['user_id'], job['last_modified_at']))
        except Exception as exc:
            log.warning("Provision failed: job=%s type=%s", job["job_id"], type(exc).__name__)
            # 用户可见错误只给出处理提示，不包含凭据或外部平台响应正文。
            message = str(exc) if isinstance(exc, RegistrationFailure) else "注册适配器执行失败，请管理员检查服务日志后重试"
            failed_phase = getattr(exc,'phase',None)
            if not failed_phase or failed_phase=='failed':failed_phase=phase
            with self.database.connect(write=True) as db:
                db.execute("UPDATE provision_jobs SET status='failed',registration_phase=?,error=?,updated_at=? WHERE id=?", (failed_phase,message, now(), job["job_id"]))
                db.execute("UPDATE profile_records SET status=?,reason=?,updated_at=? WHERE user_id=? AND last_modified_at=? AND status='creating'", ('rejected' if getattr(exc,'phase','')=='registry_rejected' else 'failed',message, now(), job["user_id"], job['last_modified_at']))
                event(db, job["user_id"], "system", "分身创建或更新失败", message, actor_id=job["reviewer"])
        return True

    # 职责：执行启动恢复及持续队列轮询。
    # 输入：由应用生命周期创建为 asyncio 后台任务。
    # 结果与约束：空队列等待 1 秒，异常等待 2 秒再轮询；取消异常向上传播以便优雅停止。
    async def run(self):
        self.recover()
        while True:
            try:
                if not await self.once():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Worker polling failed")
                await asyncio.sleep(2)
