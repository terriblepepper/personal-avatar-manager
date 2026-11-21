# 模块说明：一次性从旧资料/分身模型配置迁移账户当前设置；配置迁移不调用上游、不解密或重新加密，不删除源数据。
import json
from avatar.core.db import now


# 职责：在数据库初始化事务中集中迁移账户模型和文件说明。
# 输入：db 为初始化事务，日期表已建立且旧数据已复制。
# 结果与约束：优先保留本人已保存草稿配置，其次当前分身；保留密文字节，完成标记阻止重复回写新配置，文件说明只首次从既有引用回填。
def migrate_account_settings(db):
    if not db.execute("SELECT 1 FROM data_migrations WHERE name='independent_model_settings'").fetchone():
        tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        configs = {}
        # 当前日期表优先，旧格式只为尚未迁移的老部署提供后备；每个账户最终只有一个生效配置。
        for table in ('profile_records','avatar_records','profiles','agents','profile_submissions','submissions'):
            if table not in tables or 'model_config' not in {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}:
                continue
            order = ' ORDER BY submitted_at DESC' if table in ('profile_submissions','submissions') else ''
            for row in db.execute(f'SELECT user_id,model_config FROM {table}'+order):
                if row['user_id'] not in configs and json.loads(row['model_config'] or '{}'):
                    configs[row['user_id']] = row['model_config']
        for user_id,config in configs.items():
            db.execute('INSERT INTO model_settings(user_id,model_config,updated_at) VALUES(?,?,?)', (user_id,config,now()))
        db.execute("INSERT INTO data_migrations VALUES('independent_model_settings',?)",(now(),))
    if not db.execute("SELECT 1 FROM data_migrations WHERE name='managed_file_descriptions'").fetchone():
        seen = set()
        for table,column in (('profile_records','draft'),('avatar_records','profile'),('profile_submissions','snapshot')):
            order = ' ORDER BY submitted_at DESC' if table=='profile_submissions' else ''
            for row in db.execute(f'SELECT user_id,{column} FROM {table}'+order):
                for ref in json.loads(row[column]).get('attachments',[]):
                    if ref['id'] not in seen:
                        db.execute('UPDATE attachments SET caption=? WHERE id=? AND user_id=?',(ref.get('caption',''),ref['id'],row['user_id']))
                        seen.add(ref['id'])
        db.execute("INSERT INTO data_migrations VALUES('managed_file_descriptions',?)",(now(),))
