# 模块说明：分身业务服务：协调草稿、审核快照、后台任务和分身生效数据，路由层负责调用者权限。
import json
import uuid
from contextlib import nullcontext
from fastapi import HTTPException
from avatar.core.db import dump, event, now
from avatar.core.activity_visibility import event_scope
from avatar.core.schemas import Profile
from avatar.protocols.identity.acs import resolve_input, build_acs, public_base_url
from avatar.protocols.registration.base import RegistrationFailure
from avatar.files.library.attachments import AttachmentService
from avatar.profiles.profile_fields import public_profile, FIXED_PUBLIC_FIELDS
from avatar.profiles.profile_dates import next_modified_at
from avatar.models.model_settings import ModelSettingsService
from avatar.profiles.identity import save_identity


# 职责：协调个人资料、审核、任务、分身和平台设置业务。
# 输入：注入数据库、注册适配器和密钥保险库，路由层负责调用者身份与管理员权限检查。
# 结果与约束：草稿与审核快照、当前生效分身分离；通过事务维护状态，返回配置时隐藏凭据。
class AvatarService:
    # 职责：保存服务所需的数据库、注册器和密钥管理器。
    # 输入：database 为存储接口，provider 为注册适配器，vault 为 SecretVault。
    # 结果与约束：仅组合依赖，不触发注册或模型调用。
    def __init__(self, database, provider, vault):
        self.database, self.provider = database, provider
        self.vault = vault
        self.attachments = AttachmentService(database)
        self.models = ModelSettingsService(database,vault)

    # 职责：判断个人 ACS 是否采用本体继承的正式模式。
    # 输入：user_id 可指定既有正式分身账户。
    # 结果与约束：正式派生注册器或既有正式 AIC 均启用继承；不依据浏览器声明改变正式技能。
    def inherited_acs(self, user_id=None):
        if getattr(self.provider,'mode','demo')=='acps-direct':return False
        if getattr(self.provider, 'mode', 'demo') == 'acps-entity':
            return True
        if user_id:
            with self.database.connect() as db:
                row = db.execute('SELECT aic FROM avatar_records WHERE user_id=?', (user_id,)).fetchone()
            return bool(row and not row['aic'].startswith('DEMO-'))
        return False

    def personal_acs(self,stored,profile):
        if getattr(self.provider,'mode','demo')=='acps-direct':
            from avatar.protocols.registration.direct import personal_input
            return personal_input(stored,profile)
        return resolve_input(stored,profile)

    def deployment_acs(self, document, user_id, db=None):
        """草稿即保留固定端口；正式监听仍须审核和证书核验。"""
        settings=getattr(getattr(self.provider,'runner',None),'settings',None)
        if getattr(self.provider,'mode','demo')!='acps-direct' or settings is None:
            return document
        from avatar.protocols.transport.endpoints import entity_endpoint
        try:
            if db is None and settings.entity_aip_enabled:
                with self.database.connect() as reader:
                    endpoint = entity_endpoint(settings, user_id, db=reader, existing_only=True)
                if endpoint is None:
                    # 并发首次读取时，在写锁内重查并保留同一端口。
                    with self.database.connect(write=True) as writer:
                        endpoint = entity_endpoint(settings, user_id, db=writer)
            else:
                endpoint = entity_endpoint(settings, user_id, db=db)
        except RegistrationFailure as exc:
            raise HTTPException(409, str(exc)) from None
        document={**document,'endPoints':[endpoint] if endpoint else []}
        document.pop('webAppUrl',None)
        document['entityMeta']={**document.get('entityMeta',{}),'environment':'1.0.0'}
        if endpoint:
            from urllib.parse import urlsplit
            document['certificate']={**document['certificate'],'altNames':{'dns':[],'ip':[urlsplit(endpoint['url']).hostname]}}
        return document

    # 职责：汇总本人资料、分身、申请记录、事件和模型用量。
    # 输入：user_id 必须由已授权调用方提供。
    # 结果与约束：返回前端个人中心数据，隐藏 Key；上次修改时间直接读取资料日期，不受审核状态改变影响。
    def overview(self, user_id, *, access_scope="full"):
        with self.database.connect() as db:
            p = db.execute("SELECT * FROM profile_records WHERE user_id=?", (user_id,)).fetchone()
            agent = db.execute("SELECT * FROM avatar_records WHERE user_id=?", (user_id,)).fetchone()
            history = db.execute("SELECT s.*,u.username AS reviewer_name FROM profile_submissions s LEFT JOIN users u ON u.id=s.reviewer WHERE s.user_id=? ORDER BY submitted_at DESC", (user_id,)).fetchall()
            admin = access_scope == 'full' and bool(db.execute("SELECT role='admin' FROM users WHERE id=?", (user_id,)).fetchone()[0])
            scope,values=event_scope(user_id,admin)
            events = db.execute("SELECT e.* FROM audit_events e WHERE "+scope+" ORDER BY e.id DESC LIMIT 50",values).fetchall()
            calls = db.execute("SELECT provider,model,usage,created_at,purpose FROM model_calls WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()
            bindings=db.execute('SELECT platform,account_id,created_at FROM platform_bindings WHERE user_id=?',(user_id,)).fetchall()
            from avatar.models.embedding import EmbeddingSettings
            embedding_configured=EmbeddingSettings.configured(db,user_id)
        profile = dict(p) if p else {"draft": "{}", "last_modified_at": "", "status": "draft", "reason": ""}
        with self.database.connect() as db:
            job=db.execute('SELECT j.registration_phase,j.error,j.status FROM provision_jobs j JOIN profile_submissions s ON s.id=j.submission_id WHERE s.user_id=? ORDER BY j.updated_at DESC LIMIT 1',(user_id,)).fetchone()
        profile['registration'] = dict(job) if job else None
        profile["draft"] = json.loads(profile["draft"])
        # 当前公开策略覆盖历史私密标记，仅规范化返回副本，不篡改旧草稿及审核记录。
        profile['draft']['visibility'] = {**profile['draft'].get('visibility', {}), **{key: 'public' for key in FIXED_PUBLIC_FIELDS}}
        model_settings = self.models.get(user_id)
        profile["model_config"] = model_settings['model_config']
        stored_input = json.loads(profile.get('acs_input', '{}'))
        content = self.personal_acs(stored_input, profile['draft'])
        profile['acs_input'] = content.model_dump()
        profile['acs_generated_description']=self.personal_acs({},profile['draft']).description
        profile['acs_online']=bool(agent and agent['identity_status']=='active')
        profile['acs_active_document']=json.loads(agent['acs_document']) if profile['acs_online'] else None
        profile['acs_active_input']=None
        if profile['acs_online']:
            from avatar.protocols.registration.direct import signed_input
            active = json.loads(agent['acs_input']) or content.model_dump()
            profile['acs_active_input'] = signed_input(active,profile['acs_active_document'] or {})
        profile['acs_editing'] = bool(profile['acs_online'] and profile['status']!='active' and
            (profile.get('reuse_signed_acs')==0 or (profile.get('reuse_signed_acs') is None and
             profile['acs_active_input'].get('name')!=content.name and stored_input.get('name'))))
        inherited = self.inherited_acs(user_id)
        visible_input = profile['acs_active_input'] if profile['acs_online'] and not profile['acs_editing'] else profile['acs_input']
        if visible_input and visible_input.get('name') and not inherited:
            profile['draft']['avatar_name'] = visible_input['name']
            if not profile['acs_editing']:
                profile['acs_input']['name'] = visible_input['name']
        if profile['acs_active_document']:
            profile['acs_active_document'].pop('webAppUrl',None)
            profile['acs_active_document']['entityMeta']={**profile['acs_active_document'].get('entityMeta',{}),'environment':'1.0.0'}
            profile['acs_active_document']['active']=True
        deployment_errors=[]
        document = json.loads(profile.get('acs_document', '{}'))
        if not document:
            with self.database.connect() as db:
                document = build_acs(content, user_id, public_base_url(db), agent['aic'] if agent else '', profile.get('updated_at'))
        if p:
            try:
                document = self.deployment_acs(document, user_id)
            except HTTPException as exc:
                deployment_errors.append(str(exc.detail))
        profile['acs_document'] = document
        profile['acs_direct'] = getattr(self.provider,'mode','demo')=='acps-direct'
        if inherited:
            from avatar.protocols.identity.ontology import avatar_ontology_paths
            paths = avatar_ontology_paths(self.database.data_dir)
            path = paths.acs
            try:
                template = json.loads(path.read_text())
            except (OSError, ValueError):
                template = {}
            formal = json.loads(agent['acs_document']) if agent and not agent['aic'].startswith('DEMO-') else {}
            profile['acs_inheritance'] = {'enabled': True, 'document': formal or template, 'registered': bool(formal)}
        profile['attachments'] = self.attachments.metadata(user_id, profile['draft'].get('attachments', []))
        profile['files'] = self.attachments.inventory(user_id)
        profile['acs_validation'] = {'valid': not (content.errors()+deployment_errors), 'errors': content.errors()+deployment_errors}
        agent_view = self.decode_agent(agent)
        if agent_view is not None:
            agent_view.update(model_config=model_settings['model_config'], attachments=profile['files'])
        return {"profile": profile, "agent": agent_view, 'model_settings':model_settings,'embedding_configured':embedding_configured,
                "files":profile['files'],"files_modified_at":self.attachments.collection_date(user_id),
                "history": [self._submission(s) for s in history],
                "events": [dict(e) for e in events],
                "model_calls": [{**dict(c), "usage": json.loads(c["usage"])} for c in calls],
                'platform_bindings':[dict(r) for r in bindings]}

    # 职责：按 AIC 查询分身可公开资料。
    # 输入：aic 为目标分身身份。
    # 结果与约束：返回 AIC 与经过逐字段隐私过滤的资料；不存在返回 404，不暴露 Key、私密字段或会话历史。
    def public_avatar(self, aic):
        with self.database.connect() as db:
            row = db.execute("SELECT user_id,profile FROM avatar_records WHERE aic=?", (aic,)).fetchone()
        if not row:
            raise HTTPException(404, "分身不存在")
        profile = json.loads(row['profile'])
        visible = public_profile(profile)
        visible['attachments'] = [{**item, 'download_url': '/api/avatars/' + aic + '/attachments/' + item['id']}
                                  for item in self.attachments.public_context(row['user_id'])]
        return {'aic': aic, 'profile': visible}

    # 职责：处理待审核教师注册并记录审核结果。
    # 输入：admin 为管理员，user_id 为教师账户，decision 含通过标志和理由；权限由路由校验。
    # 结果与约束：仅处理 pending 教师，驳回理由必填；原子更新账户状态、审核记录及事件，不授予管理员身份。
    def review_account(self, admin, user_id, decision,db=None):
        reason = decision.reason.strip()
        if not decision.approve and not reason:
            raise HTTPException(422, "驳回教师注册时必须填写原因")
        with (nullcontext(db) if db is not None else self.database.connect(write=True)) as db:
            account = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not account or account["person_type"] != "teacher":
                raise HTTPException(404, "教师申请不存在")
            if account["account_status"] != "pending":
                raise HTTPException(409, "教师注册已处理")
            status = "active" if decision.approve else "rejected"
            db.execute("UPDATE users SET account_status=?,account_reason=? WHERE id=?", (status, reason, user_id))
            db.execute("INSERT INTO account_reviews(user_id,reviewer,approved,reason,created_at) VALUES(?,?,?,?,?)", (user_id, admin["id"], decision.approve, reason, now()))
            event(db, user_id, admin["username"], "教师注册审核通过" if decision.approve else "教师注册驳回", reason, actor_id=admin['id'])
        return {"status": status}

    # 职责：让被驳回的教师重新提交注册审核。
    # 输入：user 为当前教师账户。
    # 结果与约束：仅 rejected 教师可恢复 pending，清空原因并记事件；其他状态返回 409。
    def resubmit_account(self, user):
        with self.database.connect(write=True) as db:
            row = db.execute("SELECT account_status FROM users WHERE id=? AND person_type='teacher'", (user["id"],)).fetchone()
            if not row or row[0] != "rejected":
                raise HTTPException(409, "只有被驳回的教师注册可重新申请")
            db.execute("UPDATE users SET account_status='pending',account_reason='' WHERE id=?", (user["id"],))
            event(db, user["id"], user["username"], "重新提交教师注册审核", actor_id=user['id'])
        return {"status": "pending"}

    # 职责：解析数据库分身记录，排除旧模型配置副本。
    # 输入：row 为可选 SQLite Row。
    # 结果与约束：存在时返回证书和资料字典；不存在返回 None；当前模型配置由账户设置服务另行读取。
    @staticmethod
    def decode_agent(row):
        if not row:
            return None
        data=dict(row)
        data.pop('model_config',None)
        return {**data, "certificate": json.loads(row["certificate"]), "profile": json.loads(row["profile"]),
                "acs_input": json.loads(row["acs_input"]), "acs_document": json.loads(row["acs_document"])}

    # 职责：解析审核快照并脱敏模型配置。
    # 输入：row 为提交记录，包含 snapshot JSON 与审核元数据。
    # 结果与约束：返回适合前端展示的字典，保留审核元数据，不返回 API Key 明文或密文。
    @staticmethod
    def decode_submission(row):
        data=dict(row)
        data.pop('model_config',None)
        return {**data, "snapshot": json.loads(row["snapshot"]),
                "acs_input": json.loads(row["acs_input"]), "acs_document": json.loads(row["acs_document"])}

    # 职责：保存本人资料草稿，兼容旧客户端同时独立保存模型设置。
    # 输入：user 为当前账户，request 含资料日期、Profile 和旧客户端可选模型配置。
    # 结果与约束：检查状态及修改日期，允许本人选择资料身份并保持教师审核；姓名和分身名称固定公开且文字可编辑，既有学号不自动合并；加密 Key 后更新草稿，返回最新概览。
    def save_draft(self, user, request):
        with self.database.connect(write=True) as db:
            p = db.execute("SELECT * FROM profile_records WHERE user_id=?", (user["id"],)).fetchone()
            if not p:
                raise HTTPException(409, "账户资料尚未初始化，请联系管理员")
            if p["status"] in ("pending", "updating", "creating", "failed"):
                raise HTTPException(409, "当前申请正在处理，请等待审核或重试结果")
            if p["last_modified_at"] != request.last_modified_at:
                raise HTTPException(409, "资料已变更，请刷新后重新编辑")
            profile = request.profile
            if profile.attachments:
                raise HTTPException(422,'资料文件请在侧栏独立保存，无需提交资料审核')
            # 即使请求省略 visibility，保存的姓名和分身名称也明确标为公开；不限制文字编辑。
            profile.visibility.update({key: 'public' for key in FIXED_PUBLIC_FIELDS})
            save_identity(db, user, profile)
            # 兼容旧客户端的输入也立即保存到账户设置，不进入个人资料或审核快照。
            if request.model_config_input is not None:
                self.models.save_in_transaction(db,user,request.model_config_input)
            saved = json.loads(p['draft'])
            previous = self.personal_acs(json.loads(p['acs_input']), saved)
            fields = request.acs_input.model_dump() if request.acs_input else previous.model_dump()
            baseline = saved.get('avatar_name') or previous.name
            name = profile.avatar_name if 'avatar_name' in profile.model_fields_set else saved.get('avatar_name','')
            if request.acs_input and request.acs_input.name not in (baseline,name):
                if name and name!=baseline:
                    raise HTTPException(422,'同一草稿的分身名称与 ACS 名称不一致，请刷新后重试')
                name = request.acs_input.name
            fields['name'] = name
            content = self.personal_acs(fields, profile.model_dump())
            profile.avatar_name = content.name
            agent = self.decode_agent(db.execute('SELECT * FROM avatar_records WHERE user_id=?',(user['id'],)).fetchone())
            reuse = None
            if agent and agent['identity_status']=='active' and getattr(self.provider,'mode','demo')=='acps-direct':
                from avatar.protocols.registration.direct import active_input
                active = active_input(agent)
                if request.acs_edit and request.acs_input is not None:
                    reuse = int(content == active)
                else:
                    original = self.personal_acs(previous.model_dump(),profile.model_dump())
                    if request.acs_input is not None and content != original.model_copy(update={'name':content.name}):
                        raise HTTPException(409,'上线 ACS 默认只读，请先点击修改')
                    # 已保存的 ACS 修改可稍后提交；锁定的表单不因改姓名或组织而意外换证。
                    reuse = p['reuse_signed_acs'] if p['status']!='active' else 1
                    if reuse is None:reuse = int(previous == active)
                    if content.name != active.name:reuse = 0
            modified = next_modified_at(p['last_modified_at'])
            document = build_acs(content, user['id'], public_base_url(db), agent['aic'] if agent else '', modified)
            document=self.deployment_acs(document,user['id'],db)
            db.execute("UPDATE profile_records SET draft=?,acs_input=?,acs_document=?,reuse_signed_acs=?,last_modified_at=?,status='draft',reason='',updated_at=? WHERE user_id=?",
                       (dump(profile.model_dump(exclude={"attachments"})), dump(content.model_dump()), dump(agent['acs_document'] if reuse else document), reuse, modified, modified, user["id"]))
            event(db, user["id"], user["username"], "保存资料草稿", actor_id=user['id'])
        return self.overview(user["id"], access_scope=user.get("access_scope", "full"))

    # 职责：将当前修改日期对应资料生成不可变审核申请。
    # 输入：user 为注册审核已通过账户，last_modified_at 为前端读取的资料日期。
    # 结果与约束：校验日期、状态和完整性；申请使用随机 ID，提交不改变资料日期，重复或并发提交仍受状态约束。
    def submit(self, user, last_modified_at):
        if user["account_status"] != "active":
            raise HTTPException(403, "教师注册须先经管理员审核通过，才能提交分身申请")
        with self.database.connect(write=True) as db:
            p = db.execute("SELECT * FROM profile_records WHERE user_id=?", (user["id"],)).fetchone()
            if not p:
                raise HTTPException(403, "管理员不能申请分身")
            if p["status"] not in ("draft", "rejected") or p["last_modified_at"] != last_modified_at:
                raise HTTPException(409, "资料状态或修改时间已变化，请刷新")
            draft = json.loads(p['draft'])
            draft['visibility'] = {**draft.get('visibility', {}), **{key: 'public' for key in FIXED_PUBLIC_FIELDS}}
            validated = Profile(**draft)
            content = self.personal_acs(json.loads(p['acs_input']), draft)
            validated.avatar_name = content.name
            validated.validate_submission()
            if getattr(self.provider,'mode','demo')=='acps-direct':
                from avatar.protocols.registration.direct import require_profile
                require_profile(validated.model_dump())
            if content.errors() and not self.inherited_acs(user['id']):
                raise HTTPException(422, '；'.join(content.errors()))
            current = db.execute('SELECT * FROM avatar_records WHERE user_id=?', (user['id'],)).fetchone()
            document = build_acs(content, user['id'], public_base_url(db), current['aic'] if current else '')
            document=self.deployment_acs(document,user['id'],db)

            existing = current
            reuse = p['reuse_signed_acs'] if current and current['identity_status']=='active' else None
            submitted_input=content.model_dump()
            if reuse:
                from avatar.protocols.registration.direct import signed_input
                document=json.loads(current['acs_document'])
                submitted_input=signed_input(json.loads(current['acs_input']),document)
                validated.avatar_name=submitted_input.get('name') or validated.avatar_name
            db.execute("INSERT INTO profile_submissions(id,user_id,last_modified_at,snapshot,status,submitted_at,acs_input,acs_document,reuse_signed_acs) VALUES(?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), user["id"], p['last_modified_at'], dump(validated.model_dump(exclude={"attachments"})), "pending", now(), dump(submitted_input), dump(document),reuse))
            db.execute("UPDATE profile_records SET status=?,updated_at=?,reason='' WHERE user_id=?",
                       ("updating" if existing else "pending", now(), user["id"]))
            event(db, user["id"], user["username"], "提交更新审核" if existing else "提交创建审核", f"资料修改于 {p['last_modified_at']}", actor_id=user['id'])
        return self.overview(user["id"], access_scope=user.get("access_scope", "full"))

    # 职责：审核一条待处理分身资料快照。
    # 输入：user 为管理员，submission_id 为申请，decision 为通过或驳回及原因。
    # 结果与约束：拒绝重复审核；通过时同事务写入 queued 任务，驳回时保留原分身；返回申请状态，权限由路由保证。
    def review(self, user, submission_id, decision,db=None):
        reason = decision.reason.strip()
        if not decision.approve and not reason:
            raise HTTPException(422, "驳回时必须填写原因")
        with (nullcontext(db) if db is not None else self.database.connect(write=True)) as db:
            s = db.execute("SELECT * FROM profile_submissions WHERE id=?", (submission_id,)).fetchone()
            if not s:
                raise HTTPException(404, "申请不存在")
            if s["status"] != "pending":
                raise HTTPException(409, "该申请已审核")
            if decision.approve:
                content = self.personal_acs(json.loads(s['acs_input']), json.loads(s['snapshot']))
                if content.errors() and not self.inherited_acs(s['user_id']):
                    raise HTTPException(422, '；'.join(content.errors()))

            status = "approved" if decision.approve else "rejected"
            db.execute("UPDATE profile_submissions SET status=?,reason=?,reviewer=?,reviewed_at=? WHERE id=?",
                       (status, reason, user["id"], now(), submission_id))
            db.execute("UPDATE profile_records SET status=?,reason=?,updated_at=? WHERE user_id=?",
                       ("creating" if decision.approve else "rejected", reason, now(), s["user_id"]))
            if decision.approve:
                db.execute("INSERT INTO provision_jobs(id,submission_id,status,updated_at) VALUES(?,?,?,?)",
                           (str(uuid.uuid4()), submission_id, "queued", now()))
            event(db, s["user_id"], user["username"], "审核通过" if decision.approve else "审核驳回", reason, actor_id=user['id'])
        return {"status": status}

    # 职责：将等待中或失败的创建与更新任务重新加入队列。
    # 输入：user 为管理员，job_id 指定任务。
    # 结果与约束：仅 waiting/failed 任务可手动检查或重试，清理错误、同步资料状态为 creating 并记录事件；不直接调用注册器。
    def retry(self, user, job_id):
        with self.database.connect(write=True) as db:
            job = db.execute("SELECT j.*,s.user_id FROM provision_jobs j JOIN profile_submissions s ON s.id=j.submission_id WHERE j.id=?", (job_id,)).fetchone()
            if not job:
                raise HTTPException(404, "任务不存在")
            if job["status"] not in ("failed", "waiting"):
                raise HTTPException(409, "只能刷新等待中的任务或重试失败任务")
            db.execute("UPDATE provision_jobs SET status='queued',error='',updated_at=? WHERE id=?", (now(), job_id))
            db.execute("UPDATE profile_records SET status='creating',reason='',updated_at=? WHERE user_id=? AND last_modified_at=(SELECT last_modified_at FROM profile_submissions WHERE id=?)",
                       (now(), job["user_id"], job['submission_id']))
            event(db, job["user_id"], user["username"], "重试创建任务", actor_id=user['id'])
        return {"status": "queued"}

    # 职责：调用注册适配器更新指定分身证书。
    # 输入：user 为管理员，user_id 为分身所有人，路由负责授权。
    # 结果与约束：保存更新后的证书和事件并返回证书；Demo 元数据可同步续期；正式证书拒绝此同步接口，必须使用独立配置的 wit 工具。
    def renew(self, user, user_id):
        with self.database.connect(write=True) as db:
            row = db.execute("SELECT * FROM avatar_records WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, "分身不存在")
            if db.execute("SELECT 1 FROM avatar_identity_jobs WHERE user_id=? AND status IN ('queued','running','waiting')", (user_id,)).fetchone():
                raise HTTPException(409, '分身身份维护中，请等待任务完成')
            certificate = self.provider.renew(json.loads(row["certificate"]))
            db.execute("UPDATE avatar_records SET certificate=?,updated_at=? WHERE user_id=?", (dump(certificate), now(), user_id))
            event(db, user_id, user["username"], "更新演示证书", f"证书代次 {certificate['generation']}", actor_id=user['id'])
        return certificate

    # 职责：汇总管理后台统计、申请、任务、教师注册和事件。
    # 输入：当前管理员账户 ID，仅由已验证管理员的路由调用；登录审计始终限定本人。
    # 结果与约束：返回有数量上限的后台记录及配置，解码快照并脱敏 Key；mode 反映注册器配置，逐个证书仍保留其真实类型。
    def dashboard(self,user_id):
        from avatar.profiles.admin_reads import counts as admin_counts
        with self.database.connect() as db:
            counts = admin_counts(db)
            submissions = db.execute("SELECT s.*,u.username,r.username AS reviewer_name FROM profile_submissions s JOIN users u ON u.id=s.user_id LEFT JOIN users r ON r.id=s.reviewer ORDER BY s.submitted_at DESC LIMIT 500").fetchall()
            agents = db.execute("SELECT a.*,u.username FROM avatar_records a JOIN users u ON u.id=a.user_id ORDER BY a.updated_at DESC").fetchall()
            jobs = db.execute("SELECT j.*,COALESCE(NULLIF(trim(i.username),''),u.username) AS username FROM provision_jobs j JOIN profile_submissions s ON s.id=j.submission_id JOIN users u ON u.id=s.user_id LEFT JOIN oidc_identities i ON i.user_id=u.id ORDER BY j.updated_at DESC LIMIT 200").fetchall()
            identity_jobs = db.execute('SELECT j.*,u.username FROM avatar_identity_jobs j JOIN users u ON u.id=j.user_id ORDER BY j.created_at DESC LIMIT 200').fetchall()
            scope,values=event_scope(user_id,True)
            events = db.execute("SELECT e.*,u.username FROM audit_events e LEFT JOIN users u ON u.id=e.user_id WHERE "+scope+" ORDER BY e.id DESC LIMIT 200",values).fetchall()
            registrations = db.execute("SELECT u.id,u.username,u.teacher_id,u.account_status,u.account_reason,u.created_at,p.draft FROM users u LEFT JOIN profile_records p ON p.user_id=u.id WHERE u.person_type='teacher' ORDER BY u.created_at DESC").fetchall()
            account_reviews = db.execute("SELECT r.*,u.username AS applicant,a.username AS reviewer_name FROM account_reviews r JOIN users u ON u.id=r.user_id JOIN users a ON a.id=r.reviewer ORDER BY r.id DESC LIMIT 200").fetchall()
            settings = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}
        return {"counts": counts, "submissions": [self._submission(s, admin=True) for s in submissions],
                "agents": [self._agent(a, admin=True) for a in agents], "jobs": [dict(j) for j in jobs],
                "events": [dict(e) for e in events], "settings": settings, "mode": getattr(self.provider, 'mode', 'demo'),
                "registrations": [{**dict(r), "draft": json.loads(r["draft"] or "{}")} for r in registrations],
                "account_reviews": [dict(r) for r in account_reviews], 'identity_jobs': [dict(r) for r in identity_jobs]}

    # 职责：持久化管理员填写的平台服务 URL 设置。
    # 输入：user 为管理员，settings 为经过格式校验的 PlatformSettings。
    # 结果与约束：按键更新 settings 表并记录事件，返回配置；保存 URL 不代表已启用真实注册或 ACPS 通信。
    def save_settings(self, user, settings):
        with self.database.connect(write=True) as db:
            for key, value in settings.model_dump().items():
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, dump(value)))
            event(db, user["id"], user["username"], "更新平台 URL 配置", actor_id=user['id'])
        return settings.model_dump()

    # 职责：生成本人当前未保存 ACS 表单的校验预览。
    # 输入：user 为已认证账户，request 包含 ACS 内容与当前资料修改日期。
    # 结果与约束：修改日期变化返回 409，无写入、注册或模型调用；返回完整候选及可展示必填错误。
    def preview_acs(self, user, request):
        with self.database.connect(write=True) as db:
            profile = db.execute('SELECT last_modified_at,draft FROM profile_records WHERE user_id=?', (user['id'],)).fetchone()
            if not profile or profile['last_modified_at'] != request.last_modified_at:
                raise HTTPException(409, '资料已变更，请刷新后重新预览')
            agent = db.execute('SELECT aic FROM avatar_records WHERE user_id=?', (user['id'],)).fetchone()
            values = json.loads(profile['draft'])
            if request.profile_name is not None:
                values['name'] = request.profile_name.strip()
            if request.name_visibility is not None:
                values['visibility'] = {**values.get('visibility', {}), 'name': request.name_visibility}
            for key in ('avatar_name','bio','organization'):
                value=getattr(request,'profile_'+key)
                if value is not None:values[key]=value.strip()
            for key in ('bio','organization'):
                value=getattr(request,key+'_visibility')
                if value is not None:values['visibility']={**values.get('visibility',{}),key:value}
            content = self.personal_acs(request.acs_input.model_dump(), values)
            document = build_acs(content, user['id'], public_base_url(db), agent['aic'] if agent else '')
            document=self.deployment_acs(document,user['id'],db)
        errors = content.errors()
        return {'document': document,'generated_description':self.personal_acs({},values).description, 'validation': {'valid': not errors, 'errors': errors}, 'mode': getattr(self.provider, 'mode', 'demo')}

    # 职责：获取本人已保存草稿或已生效分身的 ACS，供预览和下载。
    # 输入：user 为已认证账户，source 为 draft 或 approved。
    # 结果与约束：不按外部 user_id 获取其他人的内容；旧分身无文档时提示提交更新，不临时公开或改写正式 ACS。
    def saved_acs(self, user, source):
        if source == 'draft':
            profile = self.overview(user['id'], access_scope=user.get("access_scope", "full"))['profile']
            return {'document': profile['acs_document'], 'validation': profile['acs_validation'], 'mode': getattr(self.provider, 'mode', 'demo'), 'source': source}
        with self.database.connect() as db:
            row = db.execute('SELECT acs_document FROM avatar_records WHERE user_id=?', (user['id'],)).fetchone()
        if not row or not json.loads(row[0]):
            raise HTTPException(409, '当前分身尚无已审核 ACS，请在个人资料中保存并提交更新')
        return {'document': json.loads(row[0]), 'validation': {'valid': True, 'errors': []}, 'mode': getattr(self.provider, 'mode', 'demo'), 'source': source}

    # 职责：为分身展示附加本人或管理员可查看的文件元数据。
    # 输入：row 为分身行，admin 仅由已授权管理概览传入。
    # 结果与约束：文件清单来自独立设置，不从审核快照恢复；文件路径不返回，Worker 可继续调用纯静态解码器。
    def _agent(self, row, admin=False):
        data = self.decode_agent(row)
        if data:
            data['model_config'] = self.models.get(row['user_id'])['model_config']
            data['attachments'] = self.attachments.inventory(row['user_id'],admin=admin)
        return data

    # 职责：把审核快照中的附件引用解析为可查看摘要。
    # 输入：row 为不可变申请行，admin 选择管理下载路由。
    # 结果与约束：只查询该快照指定的文件，不混入后续草稿附件或修改原件内容。
    def _submission(self, row, admin=False):
        data = self.decode_submission(row)
        data['attachments'] = self.attachments.metadata(row['user_id'], data['snapshot'].get('attachments', []), admin)
        return data
