"""业务主库连接、UTC 时间、JSON 和审计记录。"""
import json
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from avatar.core.config import require_database_url
from avatar.core.postgres import PostgresDatabase

# 审计查询统一使用 e 别名；登录记录只对本人展示，管理员也不例外。
LOGIN_EVENT_VISIBILITY = "(e.action!='统一认证登录' OR e.user_id=?)"


# 职责：生成系统统一使用的当前 UTC 时间字符串。
# 输入：无参数，使用带时区的 datetime。
# 结果与约束：返回 ISO 8601 时间，用于持久化及排序；前端负责本地显示。
def now():
    return datetime.now(timezone.utc).isoformat()


# 职责：将业务数据序列化为保留中文的 JSON。
# 输入：value 为可由 json.dumps 序列化的对象。
# 结果与约束：返回字符串，ensure_ascii=False；不可序列化数据由标准库抛出异常。
def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Database(PostgresDatabase):
    def __init__(self, data_dir, database_url=""):
        url = require_database_url(database_url)
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_dir.chmod(0o700)
        self.data_dir = data_dir
        super().__init__(url)

    @classmethod
    @contextmanager
    def open_existing(cls, data_dir, database_url):
        """运维命令只连接已引导的业务库，不在目标库创建对象。"""
        with closing(cls(data_dir, database_url)) as database:
            with database.pool.connection() as raw:
                if not database._has_root(raw):
                    raise ValueError('DATABASE_URL 未指向已初始化的业务库，请核对服务配置')
                database._load_metadata(raw)
            yield database

    def initialize(self):
        # 旧数据目录配错空库时，禁止 bootstrap 创建新管理员并覆盖首次凭据。
        legacy = self.data_dir / "avatar.sqlite3"
        users = self.data_dir / "users"
        existing = (
            (self.data_dir / "initial-admin.txt").is_file()
            or (legacy.is_file() and legacy.stat().st_size > 0)
            or (users.is_dir() and any(entry.is_dir() for entry in users.iterdir()))
        )
        try:
            super().initialize(existing_data=existing)
        except BaseException:
            self.close()
            raise


# 职责：在调用方事务中写入一条业务审计事件。
# 输入：db 为连接，user_id 为关联账户，actor、action、detail 描述操作与结果。
# 结果与约束：记录 UTC 时间，无独立提交；调用方不得将密码、Key 或临时令牌放入 detail。
def event(db, user_id, actor, action, detail="", *, actor_id=None):
    if actor_id is None and actor not in ('system','keycloak','demo-recovery','verified-platform'):
        matches=db.execute('SELECT id FROM users WHERE username=? OR id=?',(actor,actor)).fetchall()
        if len(matches)==1:actor_id=matches[0]['id']
    db.execute("INSERT INTO audit_events(user_id,actor,action,detail,created_at,actor_id) VALUES(?,?,?,?,?,?)",
               (user_id, actor, action, detail, now(), actor_id))
