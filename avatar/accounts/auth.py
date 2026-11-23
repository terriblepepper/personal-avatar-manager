# 模块说明：认证服务：密码哈希、注册、会话、CSRF、联系方式及 Demo 简化找回；平台核验由独立适配器负责。
import hashlib
import hmac
import secrets
from psycopg import IntegrityError
import time
import uuid
from fastapi import HTTPException, Request
from avatar.core.db import event, now, dump
from avatar.core.schemas import normalize_email, normalize_phone


# 职责：使用随机盐和 PBKDF2-SHA256 生成账户密码哈希。
# 输入：password 为待哈希密码；salt 可传十六进制旧盐以便验证，否则生成随机盐。
# 结果与约束：返回盐与摘要组成的字符串；执行 310000 次迭代，不保存明文密码。
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()
    return f"{salt}${digest}"


# 职责：比较输入密码与已保存密码哈希是否匹配。
# 输入：password 为输入值，hashed 为本系统生成的盐与摘要字符串。
# 结果与约束：复用原盐重新计算并使用恒定时间比较，返回布尔值；假定存储哈希结构有效。
def verify_password(password, hashed):
    salt, _ = hashed.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), hashed)


# 职责：首次初始化根管理员及其个人资料草稿。
# 输入：database 为数据库，data_dir 用于保存首次随机密码文件。
# 结果与约束：仅在不存在根管理员时创建 admin，初始密码文件设为 0600；重启不覆盖已有根账户。
def bootstrap(database, data_dir):
    with database.connect(write=True) as db:
        if not db.execute("SELECT 1 FROM users WHERE is_root=1").fetchone():
            password = secrets.token_urlsafe(18)
            user_id = str(uuid.uuid4())
            db.execute("INSERT INTO users(id,username,password,role,created_at,is_root) VALUES(?,?,?,?,?,1)", (user_id, "admin", hash_password(password), "admin", now()))
            modified = now()
            db.execute('INSERT INTO profile_records(user_id,draft,last_modified_at,updated_at) VALUES(?,?,?,?)', (user_id,dump({'person_type':'student','student_id':''}),modified,modified))
            path = data_dir / "initial-admin.txt"
            with path.open("w") as out:
                out.write(f"username: admin\npassword: {password}\n")
            path.chmod(0o600)


# 职责：注册普通学生或教师账户并初始化资料。
# 输入：credentials 为已校验的注册请求，包含登录联系方式、身份及可选平台入口令牌。
# 结果与约束：学生与教师注册后均激活；平台绑定在同一事务中完成，失败整体回滚；返回用户 ID，唯一性冲突返回 409。
def create_user(database, credentials):
    user_id = str(uuid.uuid4())
    # Reject existing contact conflicts cheaply; recheck under the write lock after hashing.
    with database.connect() as db:
        _check_contact_conflicts(db, credentials.email, credentials.phone, user_id, credentials.username)
    password_hash = hash_password(credentials.password)
    try:
        with database.connect(write=True) as db:
            _check_contact_conflicts(db, credentials.email, credentials.phone, user_id, credentials.username)
            db.execute("INSERT INTO users(id,username,password,role,created_at,person_type,student_id,teacher_id,email,phone,onboarding_stage) VALUES(?,?,?,?,?,?,?,?,?,?,'welcome')",
                       (user_id, credentials.username, password_hash, "user", now(), credentials.person_type, credentials.student_id or None, None, credentials.email, credentials.phone or None))
            modified = now()
            db.execute("INSERT INTO profile_records(user_id,draft,last_modified_at,updated_at) VALUES(?,?,?,?)", (user_id, dump({"person_type": credentials.person_type, "student_id": credentials.student_id, "name": credentials.name.strip(), "email": credentials.email, "phone": credentials.phone}), modified,modified))
            event(db, user_id, credentials.username, "账户注册", actor_id=user_id)
            if credentials.platform_entry_token:
                from avatar.accounts.platform_binding import PlatformBindingService
                PlatformBindingService(database).bind_in_transaction(db,user_id,credentials.platform_entry_token)
    except IntegrityError:
        raise HTTPException(409, "用户名、邮箱或手机号已存在，请登录已有账户或联系管理员关联平台身份")
    return user_id


# 职责：在已有数据库事务内生成服务端登录会话。
# 输入：db 为连接，user_id 指定账户；duration 为秒数，access_scope 为 full 或 profile，auth_method 标识实际认证来源。
# 结果与约束：仅保存会话令牌哈希并标记认证来源，返回原令牌、CSRF、脱敏账户及统一账号展示名/主体；identity_verified 表示账号验签登录，profile 会话隐藏管理员身份。
def issue_session(db,user_id,duration=43200,access_scope='full',auth_method='local'):
    user=db.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    if not user:raise HTTPException(404,'账户不存在')
    token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(24)
    db.execute('INSERT INTO sessions(token,user_id,csrf,expires,access_scope,auth_method) VALUES(?,?,?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user_id,csrf,time.time()+duration,access_scope,auth_method))
    result={k:user[k] for k in ('id','username','role','is_root','person_type','student_id','teacher_id','account_status','account_reason','email','phone')}
    result['onboarding_stage'] = user['onboarding_stage'] if user['role'] == 'user' else 'done'
    result['access_scope']=access_scope
    identity=db.execute('SELECT subject,username,verified_at FROM oidc_identities WHERE user_id=?',(user_id,)).fetchone()
    result['oidc_linked']=bool(identity and not user['is_root'])
    result.update(auth_source='oidc' if auth_method=='oidc' and not user['is_root'] else 'local',identity_verified=bool(identity and identity['verified_at'] and not user['is_root']),
                  oidc_subject=identity['subject'] if identity and not user['is_root'] else None,
                  identity_username=identity['username'] if identity and not user['is_root'] else '')
    # 只改变本次会话返回的有效角色，绝不改写 users 中原有管理员身份。
    if access_scope=='profile':result.update(role='user',is_root=0)
    return token,csrf,result


# 职责：按用户名、邮箱或手机号验证密码并签发完整会话。
# 输入：credentials 为登录请求，ip 用于限流，root_only 表示仅根账户可用本地密码；登录标识按联系方式规范化。
# 结果与约束：成功返回会话三元组；失败计入 15 分钟限流，用户名优先匹配，错误统一返回 401，未知账户仍计算虚拟密码哈希；统一认证启用后仅根账号可使用本地密码；关闭时预关联的旧账号仍可本地登录。
def login(database, credentials, ip, root_only=False):
    identifier = credentials.username.strip()
    try:
        identifier = normalize_email(identifier) if '@' in identifier else normalize_phone(identifier)
    except ValueError:
        identifier = credentials.username.strip()
    key = ip + ":" + identifier
    current = time.time()
    with database.connect(write=True) as db:
        limit = db.execute("SELECT * FROM login_limits WHERE key=?", (key,)).fetchone()
        if limit and limit["reset_at"] > current and limit["attempts"] >= 10:
            raise HTTPException(429, "登录尝试过多，请 15 分钟后重试")
        # 优先匹配原用户名，再匹配规范化邮箱或手机号；注册时已防止别名归属冲突。
        user = db.execute("SELECT * FROM users WHERE username=?", (credentials.username.strip(),)).fetchone()
        if not user:
            user = db.execute("SELECT * FROM users WHERE email=? OR phone=?", (identifier, identifier)).fetchone()
        key = ip + ":" + (user["id"] if user else identifier)
        limit = db.execute("SELECT * FROM login_limits WHERE key=?", (key,)).fetchone()
        if limit and limit["reset_at"] > current and limit["attempts"] >= 10:
            raise HTTPException(429, "登录尝试过多，请 15 分钟后重试")
        managed = bool(user and not user['is_root'] and db.execute('SELECT 1 FROM oidc_identities WHERE user_id=?', (user['id'],)).fetchone())
        valid = verify_password(credentials.password, user["password"] if user else hash_password("dummy-password", "00"*16))
        if not user or not valid or (managed and root_only) or (root_only and not user['is_root']):
            attempts = limit["attempts"] + 1 if limit and limit["reset_at"] > current else 1
            reset = limit["reset_at"] if limit and limit["reset_at"] > current else current + 900
            db.execute("INSERT OR REPLACE INTO login_limits VALUES(?,?,?)", (key, attempts, reset))
        else:
            db.execute("DELETE FROM login_limits WHERE key=?", (key,))
            db.execute("DELETE FROM sessions WHERE expires < ?", (current,))
            return issue_session(db,user['id'])
    raise HTTPException(401, "登录账号或密码不正确")


# 职责：解析请求 Cookie 并检查登录、CSRF 和临时会话范围。
# 输入：request 提供 Cookie、请求方法、路径和 x-csrf-token；账户角色每次从数据库读取。
# 结果与约束：返回当前用户字典；未登录 401、校验失败 403；profile 会话只能访问白名单资料接口且不携带管理权限。
def current_user(request: Request):
    token = request.cookies.get("avatar_session", "")
    with request.app.state.database.connect() as db:
        row = db.execute("SELECT u.id,u.username,u.role,u.is_root,u.person_type,u.student_id,u.teacher_id,u.account_status,u.account_reason,u.email,u.phone,u.onboarding_stage,s.csrf,s.access_scope,s.auth_method FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?",
                         (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    if not row:
        raise HTTPException(401, "请先登录")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf"]):
            raise HTTPException(403, "会话校验失败，请刷新页面")
    user=dict(row)
    if user['role'] == 'admin':
        user['onboarding_stage'] = 'done'
    with request.app.state.database.connect() as db:
        identity=db.execute('SELECT subject,username,verified_at FROM oidc_identity_links WHERE user_id=? AND issuer=?',
                            (user['id'], request.app.state.settings.oidc_issuer.rstrip('/'))).fetchone()
        if not identity:
            identity=db.execute('SELECT subject,username,verified_at FROM oidc_identities WHERE user_id=?',(user['id'],)).fetchone()
    user['oidc_linked']=bool(identity and not user['is_root'])
    user.update(auth_source='oidc' if user['auth_method']=='oidc' and not user['is_root'] else 'local',identity_verified=bool(identity and identity['verified_at'] and not user['is_root']),
                oidc_subject=identity['subject'] if identity and not user['is_root'] else None,
                identity_username=identity['username'] if identity and not user['is_root'] else '')
    if request.app.state.settings.oidc_enabled and not user['is_root'] and user['access_scope']=='full' and user['auth_method']!='oidc':
        raise HTTPException(401, '请通过统一认证重新登录')
    # 入口会话使用路径白名单缩小能力，即便关联的是根管理员，也不能继承完整控制台权限。
    if user['access_scope']=='profile':
        allowed={'/api/me/onboarding/tour','/api/me/onboarding/done','/api/help/用户操作手册.md','/api/records/history','/api/records/events','/api/auth/me','/api/auth/logout','/api/me','/api/me/profile','/api/me/submit','/api/me/acs','/api/me/acs/preview','/api/me/attachments','/api/me/material-folders','/api/me/model-settings','/api/me/model-options','/api/profile-fields','/api/model-providers','/api/platform/binding'}
        import re
        attachment_path = re.fullmatch(r'/api/me/attachments/[0-9a-f-]{36}', request.url.path)
        if request.url.path not in allowed and not attachment_path:raise HTTPException(403,'临时入口仅允许个人资料管理；其他操作请使用账号密码登录')
        user.update(role='user',is_root=0)
    return user


# 职责：检查当前账户是否具有管理员角色。
# 输入：user 为已认证账户字典，要求 role 为 admin。
# 结果与约束：通过时无返回值，否则返回 403；根身份之外的业务限制由具体操作另行检查。
def require_admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")


# 职责：验证当前密码后修改本人密码。
# 输入：user 为已登录账户，body 含当前密码与新密码。
# 结果与约束：拒绝错误旧密码及相同新密码；事务内撤销全部会话和未使用入口并记事件，返回重新登录提示。
def change_password(database, user, body):
    if user.get('auth_source') == 'oidc':
        raise HTTPException(403, '登录凭据由 Keycloak 管理，请前往统一账户中心修改')
    with database.connect(write=True) as db:
        row = db.execute("SELECT password FROM users WHERE id=?", (user["id"],)).fetchone()
        if not verify_password(body.current_password, row["password"]):
            raise HTTPException(400, "当前密码不正确")
        if body.current_password == body.new_password:
            raise HTTPException(422, "新密码不能与当前密码相同")
        _replace_password(db, user["id"], body.new_password)
        event(db, user["id"], user["username"], "修改账户密码", actor_id=user['id'])
    return {"message": "密码已修改，请使用新密码重新登录"}


# 职责：在调用方事务内更新密码并撤销旧认证凭据。
# 输入：db 为写事务连接，user_id 为目标账户，password 为新明文密码。
# 结果与约束：存入新哈希，删除该用户全部会话，消费未使用的已绑定入口，并清理按账户限流记录；不单独提交事务。
def _replace_password(db, user_id, password):
    db.execute("UPDATE users SET password=? WHERE id=?", (hash_password(password), user_id))
    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    db.execute('UPDATE platform_entry_tokens SET used_at=? WHERE issued_for_user_id=? AND used_at IS NULL',(now(),user_id))
    # 找回后清理按账户累计的登录限制，使持有人可使用新密码重新登录。
    db.execute("DELETE FROM login_limits WHERE key LIKE ?", ("%:"+user_id,))


# 职责：按 Demo 简化需求匹配注册联系方式并重置密码。
# 输入：body 含规范化注册邮箱或手机号及新密码，ip 用于找回限流。
# 结果与约束：不验证联系方式持有人，排除根账户及 OIDC 账户；每 15 分钟最多 5 次，成功撤销旧会话和入口。
def recover_password(database, body, ip):
    """Explicitly simplified demo recovery: matching registered contact only.

    Not possession verification; replace this boundary with platform recovery when
    deploying beyond the demo. No profile contact or arbitrary username can reset.
    """
    current = time.time()
    key = "recover:" + ip
    limited = False
    with database.connect(write=True) as db:
        limit = db.execute("SELECT * FROM recovery_limits WHERE key=?", (key,)).fetchone()
        attempts = limit["attempts"] + 1 if limit and limit["reset_at"] > current else 1
        reset = limit["reset_at"] if limit and limit["reset_at"] > current else current+900
        db.execute("INSERT OR REPLACE INTO recovery_limits VALUES(?,?,?)", (key, attempts, reset))
        limited = attempts > 5
        if not limited:
            column = "email" if '@' in body.contact else "phone"
            row = db.execute(f"SELECT id,username FROM users WHERE {column}=? AND is_root=0 AND NOT EXISTS (SELECT 1 FROM oidc_identities o WHERE o.user_id=users.id)", (body.contact,)).fetchone()
            if row:
                _replace_password(db, row["id"], body.new_password)
                event(db, row["id"], "demo-recovery", "通过注册联系方式重置密码")
    if limited:
        raise HTTPException(429, "找回尝试过多，请 15 分钟后重试")
    # 联系方式未匹配也返回相同提示，避免通过找回响应公开枚举账户。
    return {"message": "如联系方式与注册账户匹配，密码已重置，请使用新密码登录"}


# 职责：检查登录邮箱、手机号及用户名别名是否冲突。
# 输入：db 为事务连接；email、phone 为规范化联系方式，user_id 排除本人，username 为可选注册用户名。
# 结果与约束：冲突返回 409，防止手机号及其国际格式与用户名指向不同账户；无冲突不修改数据。
def _check_contact_conflicts(db, email, phone, user_id, username=None):
    if db.execute("SELECT 1 FROM users WHERE id<>? AND (email=? OR (?<>'' AND phone=?))", (user_id, email, phone, phone)).fetchone():
        raise HTTPException(409, "邮箱或手机号已被其他账户使用")
    if phone:
        aliases = (phone, "+86"+phone, "0086"+phone)
        if db.execute("SELECT 1 FROM users WHERE id<>? AND username IN (?,?,?)", (user_id, *aliases)).fetchone():
            raise HTTPException(409, "手机号与已有用户名冲突，请使用其他联系方式")
    if username:
        try:
            alias = normalize_phone(username)
        except ValueError:
            alias = username
        if db.execute("SELECT 1 FROM users WHERE phone=?", (alias,)).fetchone():
            raise HTTPException(409, "用户名与已有手机号冲突")


# 职责：验证当前密码并更新本人登录联系方式。
# 输入：user 为当前账户，body 含规范化邮箱、可选手机号及当前密码。
# 结果与约束：检查唯一性与别名冲突后更新 users 并记事件；不会同步覆盖个人资料公开联系方式。
def change_contacts(database, user, body):
    if user.get('auth_source') == 'oidc':
        raise HTTPException(403, '登录手机号由 Keycloak 管理，请前往统一账户中心换绑')
    if user.get('is_root') and body.phone:
        raise HTTPException(403, '根管理员使用本地账号，不绑定手机号')
    try:
        with database.connect(write=True) as db:
            row = db.execute("SELECT password FROM users WHERE id=?", (user["id"],)).fetchone()
            if not verify_password(body.current_password, row["password"]):
                raise HTTPException(400, "当前密码不正确")
            _check_contact_conflicts(db, body.email, body.phone, user["id"])
            db.execute("UPDATE users SET email=?,phone=? WHERE id=?", (body.email, body.phone or None, user["id"]))
            event(db, user["id"], user["username"], "修改登录联系方式", actor_id=user['id'])
    except IntegrityError:
        raise HTTPException(409, "邮箱或手机号已被其他账户使用")
    return {"message": "登录联系方式已保存"}
