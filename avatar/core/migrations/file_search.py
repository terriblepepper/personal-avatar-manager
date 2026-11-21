# 模块说明：文件提取代次仅用于升级旧 PDF 解析缓存，不影响个人资料日期、隐私或审核。


# 职责：为两类原件添加解析缓存代次；旧行保留并在检索时按需重新提取，不删除业务资料。
def migrate_file_search(db):
    for table in ('attachments','chat_files'):
        if 'extraction_version' not in {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}:
            db.execute('ALTER TABLE '+table+' ADD COLUMN extraction_version INTEGER NOT NULL DEFAULT 0')
