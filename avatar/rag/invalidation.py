"""模型配置变更的索引失效通知；由调用方事务提交，不加载索引器。"""
import uuid


def invalidate(db,user_id,*,force=False):
    if force:
        epoch=uuid.uuid4().hex
        db.execute('INSERT OR REPLACE INTO file_rag_epochs VALUES(?,?)',(user_id,epoch))
        db.execute("UPDATE file_rag_documents SET cache_epoch=?,force_rebuild=1 WHERE user_id=?",(epoch,user_id))
    db.execute("UPDATE file_rag_documents SET status='queued',error='',generation=generation+1 WHERE user_id=?",(user_id,))
