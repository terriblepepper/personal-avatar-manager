# 模块说明：根管理员审核后的旧账号迁移；以稳定统一身份关联原业务账号，支持显式回收无业务数据的重复占位账号。
import hashlib
import json
import secrets
from psycopg import IntegrityError
import time
import uuid
from fastapi import HTTPException
from avatar.core.db import dump, event, now
from avatar.accounts.management import AccountManagement


# 职责：提供迁移列表、只读预览和一次性执行边界。
# 输入：应用数据库及固定认证配置；不接受浏览器指定其他 issuer。
# 结果与约束：仅根管理员完整会话可迁移，不推测手机号归属，不合并两份业务资料，不改根身份或远端 Keycloak 数据。
class IdentityMigrationService:
    # 职责：保存迁移依赖。
    # 输入：database 和 settings 为当前应用共享配置。
    # 结果与约束：不联网、不自动迁移；统一认证关闭时允许提前关联并保留原本地登录。
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.issuer = settings.oidc_issuer.rstrip('/')

    # 职责：在当前事务中重新确认操作者的根权限。
    # 输入：db 是数据库连接，actor 是经过会话依赖取得的用户。
    # 结果与约束：不信任传入的 is_root/role 缓存；普通管理员、已降级及临时资料会话返回 403。
    @staticmethod
    def require_root(db, actor):
        user = db.execute('SELECT role,is_root FROM users WHERE id=?', (actor['id'],)).fetchone()
        if not user or user['role'] != 'admin' or not user['is_root'] or actor.get('access_scope', 'full') != 'full':
            raise HTTPException(403, '账号迁移仅允许根管理员完整登录后操作')

    # 职责：列出迁移候选及关联进度。
    # 输入：actor、搜索文字及分页偏移。
    # 结果与约束：复用账号分页，返回身份关联状态和固定 issuer，根账号保留展示但不可迁移，不返回凭据。
    def inventory(self, actor, search='', offset=0):
        with self.database.connect() as db:
            self.require_root(db, actor)
        result = AccountManagement(self.database).list(actor, search, 'all', offset)
        with self.database.connect() as db:
            self.require_root(db, actor)
            for user in result['items']:
                identity = db.execute('SELECT issuer,subject,username,verified_at FROM oidc_identities WHERE user_id=?', (user['id'],)).fetchone()
                previous = identity
                current = db.execute('SELECT issuer,subject,username,verified_at FROM oidc_identity_links WHERE user_id=? AND issuer=?',(user['id'],self.issuer)).fetchone()
                identity = current or (identity if identity and identity['issuer']==self.issuer else None)
                user['previous_identity'] = dict(previous) if previous and previous['issuer']!=self.issuer else None
                user['identity'] = dict(identity) if identity else None
                user['migration_status'] = ('local_root' if user['is_root'] else 'verified' if identity and identity['verified_at'] else 'pending' if identity else 'unlinked')
        return {**result, 'issuer': self.issuer, 'oidc_enabled': self.settings.oidc_enabled}

    # 职责：在同一读取事务中汇总目标业务资产。
    # 输入：db 和 user_id，不接收外部表名。
    # 结果与约束：返回资料、技能和双边协商计数及集合修改标记，供预览/冲突指纹使用，不返回模型密文或聊天原文。
    @staticmethod
    def summary(db, user_id):
        user = db.execute('SELECT id,username,role,is_root,person_type,phone,account_status FROM users WHERE id=?', (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, '原账号不存在')
        p = db.execute('SELECT draft,last_modified_at,status,acs_input,acs_document FROM profile_records WHERE user_id=?', (user_id,)).fetchone()
        agent = db.execute('SELECT aic,last_modified_at FROM avatar_records WHERE user_id=?', (user_id,)).fetchone()
        result = {'account': dict(user), 'name': json.loads(p['draft']).get('name', '') if p else '',
                  'profile_modified_at': p['last_modified_at'] if p else None, 'profile_status': p['status'] if p else None,
                  'aic': agent['aic'] if agent else None, 'agent_modified_at': agent['last_modified_at'] if agent else None}
        for key, table in [('submissions', 'profile_submissions'), ('files', 'attachments'), ('messages', 'chat_messages'), ('conversations', 'chat_conversations'), ('model_settings', 'model_settings'), ('model_calls', 'model_calls'), ('platform_bindings', 'platform_bindings'), ('account_reviews', 'account_reviews'), ('skills', 'agent_skills'), ('chat_files', 'chat_files')]:
            result[key] = db.execute('SELECT count(*) FROM ' + table + ' WHERE user_id=?', (user_id,)).fetchone()[0]
        for key, table in [('files_modified_at', 'attachment_collections'), ('model_modified_at', 'model_settings'), ('skills_modified_at', 'agent_skill_collections')]:
            row = db.execute('SELECT updated_at FROM ' + table + ' WHERE user_id=?', (user_id,)).fetchone()
            result[key] = row['updated_at'] if row else None
        result['dialogues']=db.execute('SELECT count(*) FROM agent_dialogues WHERE user_id=? OR target_user_id=?',(user_id,user_id)).fetchone()[0]
        result['dialogues_modified_at']=db.execute('SELECT max(updated_at) FROM agent_dialogues WHERE user_id=? OR target_user_id=?',(user_id,user_id)).fetchone()[0]
        identity = db.execute('SELECT issuer,subject,username,phone,verified_at FROM oidc_identities WHERE user_id=?', (user_id,)).fetchone()
        result['identity'] = dict(identity) if identity else None
        return result

    # 职责：确认重复业务账号仅为 OIDC 首次登录生成的空占位。
    # 输入：db、账号 ID 和已读取的业务汇总。
    # 结果与约束：任何编辑、审核、文件、模型、技能安装历史、协商、有效会话、权限或平台绑定阻止清理；只允许初始草稿、登录审计和可确认的系统默认空会话。
    @staticmethod
    def require_empty(db, user_id, summary):
        user = summary['account']
        if db.execute('SELECT 1 FROM chat_generation_jobs WHERE user_id=?',(user_id,)).fetchone():
            raise HTTPException(409,'账号已有聊天请求记录，不能作为空占位账号清理')
        if db.execute('SELECT 1 FROM material_folders WHERE user_id=?',(user_id,)).fetchone():
            raise HTTPException(409,'账号已有资料文件夹，不能作为空占位账号清理')
        if db.execute('SELECT 1 FROM oidc_identity_links WHERE user_id=?', (user_id,)).fetchone():
            raise HTTPException(409, '账号已有跨 issuer 的可信身份链接，不能清理为占位账号')
        if user['is_root'] or user['role'] != 'user' or not user['username'].startswith('kc_') or summary['aic'] or any(summary[k] for k in ('submissions', 'files', 'messages', 'model_settings', 'model_calls', 'platform_bindings', 'account_reviews', 'skills', 'chat_files', 'dialogues')) or summary['skills_modified_at']:
            raise HTTPException(409, '该 Keycloak 身份已有业务资料，不能自动合并；请保留两边数据并人工核对')
        for conversation in db.execute('SELECT * FROM chat_conversations WHERE user_id=?', (user_id,)):
            expected = str(uuid.uuid5(uuid.NAMESPACE_URL, 'avatar-default:' + dump([conversation['platform'], user_id, conversation['channel'], conversation['target_aic'], conversation['caller_id']])))
            if (conversation['id'] != expected or conversation['title'] != '默认会话' or conversation['platform'] != 'web'
                    or conversation['caller_id'] != user_id or conversation['parent_id'] or conversation['selected_aic']
                    or conversation['summary'] or conversation['summary_through'] or conversation['compression_count']
                    or conversation['summary_profile_modified_at'] or conversation['lease_until']
                    or db.execute('SELECT 1 FROM chat_messages WHERE conversation_id=?', (conversation['id'],)).fetchone()
                    or db.execute('SELECT 1 FROM chat_conversations WHERE parent_id=?', (conversation['id'],)).fetchone()):
                raise HTTPException(409, '占位账号已有有效或自建会话，不能清理')
        p = db.execute('SELECT * FROM profile_records WHERE user_id=?', (user_id,)).fetchone()
        if not p or p['status'] != 'draft' or json.loads(p['acs_input']) or json.loads(p['acs_document']):
            raise HTTPException(409, '占位账号已有资料变更，不能清理')
        draft = json.loads(p['draft'])
        if set(draft) - {'person_type', 'name', 'student_id'} or draft.get('student_id'):
            raise HTTPException(409, '占位账号已有资料变更，不能清理')
        if db.execute("SELECT 1 FROM audit_events WHERE user_id=? AND action NOT IN ('统一认证首次建立业务账户','统一认证登录')", (user_id,)).fetchone():
            raise HTTPException(409, '占位账号有其他操作历史，不能清理')
        for sql in ('SELECT 1 FROM account_reviews WHERE reviewer=?', 'SELECT 1 FROM profile_submissions WHERE reviewer=?', 'SELECT 1 FROM platform_entry_tokens WHERE issued_for_user_id=?', 'SELECT 1 FROM chat_conversations WHERE caller_id=? AND user_id<>caller_id'):
            if db.execute(sql, (user_id,)).fetchone():
                raise HTTPException(409, '占位账号存在其他业务引用，不能清理')

    # 职责：生成当前关联计划，不写业务身份。
    # 输入：目标账号、稳定 Keycloak User ID，以及是否显式允许转移空占位身份。
    # 结果与约束：同一关联幂等；根目标、已验证的其他主体、非空重复账号和正在生成回答均拒绝；未验证误填别名仅在已登录身份匹配且原占位可清理时允许修正。
    def plan(self, db, user_id, subject, allow_empty_transfer):
        target = self.summary(db, user_id)
        if target['account']['is_root']:
            raise HTTPException(403, '根管理员保留本地登录，不参与迁移')
        primary = target['identity']
        linked = db.execute('SELECT issuer,subject,username,NULL AS phone,verified_at FROM oidc_identity_links WHERE user_id=? AND issuer=?',(user_id,self.issuer)).fetchone()
        cross_issuer = bool(primary and primary['issuer'] != self.issuer)
        existing = dict(linked) if linked else (None if cross_issuer else primary)
        target['current_identity'] = existing
        if db.execute('SELECT 1 FROM chat_conversations WHERE user_id=? AND lease_until>?', (user_id, time.time())).fetchone():
            raise HTTPException(409, '原账号正在生成回答，请结束后再迁移')
        occupants = db.execute('SELECT user_id FROM oidc_identities WHERE issuer=? AND subject=? UNION SELECT user_id FROM oidc_identity_links WHERE issuer=? AND subject=?',(self.issuer,subject,self.issuer,subject)).fetchall()
        if len(occupants)>1:raise HTTPException(409,'统一身份关联冲突，需人工核对')
        occupied = occupants[0] if occupants else None
        placeholder = None
        if occupied and occupied['user_id'] != user_id:
            placeholder = self.summary(db, occupied['user_id'])
            self.require_empty(db, occupied['user_id'], placeholder)
            if not allow_empty_transfer:
                raise HTTPException(409, '统一身份已有空业务账号，请勾选显式转移选项后重新预览')
        # 仅修正从未成功登录、误填为该已验签账号展示名的旧关联；任何已经验证的主体都不能覆盖。
        repair_alias = bool(existing and (existing['issuer'], existing['subject']) != (self.issuer, subject))
        if repair_alias and not (placeholder and existing['issuer'] == self.issuer and not existing['verified_at']
                                 and placeholder['identity']['verified_at'] and existing['subject'] == placeholder['identity'].get('username')):
            raise HTTPException(409, '原账号已关联其他统一身份，不能覆盖')
        return {'issuer': self.issuer, 'subject': subject, 'target': target, 'empty_account': placeholder,
                'repair_alias': repair_alias, 'cross_issuer': cross_issuer,
                'action': 'transfer_empty' if placeholder else 'already_linked' if existing else 'link_issuer' if cross_issuer else 'associate',
                'oidc_enabled': self.settings.oidc_enabled}

    # 职责：保存五分钟一次性的根管理员预览凭据。
    # 输入：actor 和目标账号、Keycloak User ID 或已验签登录的用户名、显式清理选项。
    # 结果与约束：用户名只从唯一已登录身份解析；首次关联要求标准 User ID，误填且未验证的别名仅能经空账号核验修正。
    def preview(self, actor, body):
        with self.database.connect(write=True) as db:
            self.require_root(db, actor)
            # 根管理员可输入已登录的用户名；只从已验签身份缓存唯一解析，不按业务邮箱推测账号。
            matches = db.execute('SELECT subject FROM oidc_identities WHERE issuer=? AND username=? AND verified_at IS NOT NULL UNION SELECT subject FROM oidc_identity_links WHERE issuer=? AND username=? AND verified_at IS NOT NULL', (self.issuer, body.subject,self.issuer,body.subject)).fetchall()
            exact = db.execute('SELECT verified_at FROM oidc_identities WHERE issuer=? AND subject=?', (self.issuer, body.subject)).fetchone()
            if len(matches) > 1:
                raise HTTPException(409, '该用户名对应多个已登录身份，请核对并填写准确 Keycloak User ID')
            canonical = matches[0]['subject'] if matches else body.subject
            if not matches and not (exact and exact['verified_at']):
                try:
                    uuid.UUID(canonical)
                except ValueError:
                    raise HTTPException(422, '请输入 Keycloak 用户详情中的 User ID；使用用户名时须让该账号先登录一次') from None
            plan = self.plan(db, body.user_id, canonical, body.allow_empty_transfer)
            token = secrets.token_urlsafe(32)
            db.execute('DELETE FROM identity_migration_previews WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO identity_migration_previews VALUES(?,?,?,?,?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), actor['id'], body.user_id, canonical,
                        int(body.allow_empty_transfer), self.issuer, hashlib.sha256(dump(plan).encode()).hexdigest(), time.time() + 300))
        return {**plan, 'preview_token': token, 'expires_in': 300}

    # 职责：执行核验并预览过的迁移计划。
    # 输入：actor、一次性预览凭据、明确归属核验确认及备注。
    # 结果与约束：写事务内再次检查根权限、过期及指纹；仅关联或转移空占位，原业务资产/角色不覆盖；启用 OIDC 时撤销旧会话。
    def commit(self, actor, body):
        try:
            with self.database.connect(write=True) as db:
                self.require_root(db, actor)
                key = hashlib.sha256(body.preview_token.encode()).hexdigest()
                preview = db.execute('SELECT * FROM identity_migration_previews WHERE token_hash=?', (key,)).fetchone()
                if not preview or preview['actor_id'] != actor['id'] or preview['expires'] <= time.time() or preview['issuer'] != self.issuer:
                    raise HTTPException(410, '迁移预览已失效，请重新预览')
                plan = self.plan(db, preview['user_id'], preview['subject'], bool(preview['allow_empty_transfer']))
                if hashlib.sha256(dump(plan).encode()).hexdigest() != preview['fingerprint']:
                    raise HTTPException(409, '账号或业务数据在预览后已变化，请重新预览')
                target_id = preview['user_id']
                if plan['action'] == 'transfer_empty':
                    source_id = plan['empty_account']['account']['id']
                    if plan['repair_alias']:
                        db.execute('DELETE FROM oidc_identities WHERE user_id=?', (target_id,))
                    if plan['cross_issuer']:
                        db.execute('INSERT INTO oidc_identity_links(issuer,subject,user_id,username,evidence) VALUES(?,?,?,?,?)',(self.issuer,preview['subject'],target_id,plan['empty_account']['identity']['username'],body.reason))
                        db.execute('DELETE FROM oidc_identities WHERE user_id=?',(source_id,))
                    else:
                        db.execute('UPDATE oidc_identities SET user_id=?,phone=NULL,verified_at=NULL WHERE user_id=?', (target_id, source_id))
                    db.execute('DELETE FROM sessions WHERE user_id=?', (source_id,))
                    db.execute('UPDATE login_devices SET user_id=? WHERE user_id=? AND NOT EXISTS (SELECT 1 FROM login_devices existing WHERE existing.user_id=? AND existing.device_hash=login_devices.device_hash)', (target_id, source_id, target_id))
                    db.execute('DELETE FROM login_devices WHERE user_id=?', (source_id,))
                    db.execute('DELETE FROM chat_conversations WHERE user_id=?', (source_id,))
                    db.execute('DELETE FROM profile_records WHERE user_id=?', (source_id,))
                    db.execute('DELETE FROM attachment_collections WHERE user_id=?', (source_id,))
                    db.execute('UPDATE audit_events SET user_id=? WHERE user_id=?', (target_id, source_id))
                    db.execute('UPDATE audit_events SET actor_id=? WHERE actor_id=?', (target_id, source_id))
                    db.execute('DELETE FROM identity_migration_previews WHERE user_id=?', (source_id,))
                    db.execute('DELETE FROM users WHERE id=?', (source_id,))
                elif plan['action'] == 'link_issuer':
                    db.execute('INSERT INTO oidc_identity_links(issuer,subject,user_id,evidence) VALUES(?,?,?,?)',(self.issuer,preview['subject'],target_id,body.reason))
                elif plan['action'] == 'associate':
                    db.execute('INSERT INTO oidc_identities(issuer,subject,user_id) VALUES(?,?,?)', (self.issuer, preview['subject'], target_id))
                if self.settings.oidc_enabled and plan['action'] != 'already_linked':
                    db.execute('DELETE FROM sessions WHERE user_id=?', (target_id,))
                    db.execute('UPDATE platform_entry_tokens SET used_at=? WHERE issued_for_user_id=? AND used_at IS NULL', (now(), target_id))
                db.execute('DELETE FROM identity_migration_previews WHERE token_hash=?', (key,))
                if plan['action'] != 'already_linked':
                    event(db, target_id, actor['id'], '旧账号迁移关联', dump({'issuer': self.issuer, 'subject': preview['subject'], 'previous_issuer': plan['target']['identity']['issuer'] if plan['cross_issuer'] else None, 'action': plan['action'], 'previous_subject': plan['target']['identity']['subject'] if plan['repair_alias'] else None, 'reason': body.reason}), actor_id=actor['id'])
                return {'message': '迁移关联已保存；统一认证开启后使用 Keycloak 账号进入原账号' if not self.settings.oidc_enabled else '迁移完成，请通过 Keycloak 重新登录原账号', 'user_id': target_id, 'action': plan['action']}
        except IntegrityError:
            raise HTTPException(409, '关联或业务引用已变化，未迁移或删除任何账号，请重新预览') from None
