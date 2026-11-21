# 模块说明：将附件可见性和生效状态迁移到独立文件管理，不让旧资料审核覆盖用户当前文件设置。
import json
from avatar.core.db import now


# 职责：首次迁移独立文件设置及账户文件修改日期。
# 输入：db 为初始化事务，旧附件说明和资料日期迁移已经完成。
# 结果与约束：保留原件、说明和归属；仅继承当前分身已生效的公开设置，其余文件默认私密；空说明保持暂存，重复初始化不覆盖新设置。
def migrate_independent_attachments(db):
    if db.execute("SELECT 1 FROM data_migrations WHERE name='independent_attachments'").fetchone():
        return
    for owner in db.execute('SELECT DISTINCT user_id FROM attachments').fetchall():
        agent=db.execute('SELECT profile FROM avatar_records WHERE user_id=?',(owner['user_id'],)).fetchone()
        approved={ref['id']:ref for ref in json.loads(agent['profile']).get('attachments',[])} if agent else {}
        modified=now()
        for row in db.execute('SELECT id,caption,saved_at FROM attachments WHERE user_id=?',(owner['user_id'],)).fetchall():
            visibility=approved.get(row['id'],{}).get('visibility','private')
            saved=row['saved_at'] if row['caption'].strip() else None
            db.execute('UPDATE attachments SET visibility=?,saved_at=?,updated_at=? WHERE id=?',(visibility,saved,modified,row['id']))
        db.execute('INSERT INTO attachment_collections(user_id,updated_at) VALUES(?,?)',(owner['user_id'],modified))
    db.execute("INSERT INTO data_migrations VALUES('independent_attachments',?)",(now(),))
