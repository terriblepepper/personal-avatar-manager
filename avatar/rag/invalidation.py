"""模型配置变更的索引失效通知；由调用方事务提交，不加载索引器。"""
import hashlib
import uuid

from avatar.core.db import dump


def invalidate(db,user_id,*,force=False):
    if force:
        epoch=uuid.uuid4().hex
        db.execute('INSERT OR REPLACE INTO file_rag_epochs VALUES(?,?)',(user_id,epoch))
        db.execute("UPDATE file_rag_documents SET cache_epoch=?,force_rebuild=1 WHERE user_id=?",(epoch,user_id))
    db.execute("UPDATE file_rag_documents SET status='queued',error='',generation=generation+1 WHERE user_id=?",(user_id,))


def signature(embedding,model):
    from avatar.models.embedding import index_identity
    return hashlib.sha256(dump(['file-index-v3',index_identity(embedding),modalities(model)]).encode()).hexdigest()


def modalities(config):
    return tuple(bool(config.get(key)) for key in ('vision_enabled','audio_enabled'))


def chat_changed(db,user_id,previous,config):
    old,new=modalities(previous),modalities(config)
    if old!=new:
        invalidate(db,user_id)
        if old[0]!=new[0]:
            from avatar.knowledge.model_refresh import schedule
            schedule(db,user_id,visual=True)
        return
