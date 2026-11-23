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


def legacy_signature(embedding,model):
    fields=('provider','base_url','model','dimensions','context_tokens','context_enabled','multimodal_enabled')
    # The historical parser signature keeps previously published embeddings valid.
    return hashlib.sha256(dump(['unstructured-chroma-document-images-v2',{key:embedding.get(key) for key in fields},
        {key:model.get(key) for key in ('provider','base_url','model','vision_enabled','audio_enabled')}]).encode()).hexdigest()


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
    if all(previous.get(name)==config.get(name) for name in ('provider','base_url','model')):
        return
    from avatar.models.embedding import EmbeddingSettings
    embedding=EmbeddingSettings.current(db,user_id)
    if embedding:
        # Re-label an existing compatible publication once, without touching its vectors.
        db.execute("UPDATE file_rag_documents SET signature=? WHERE user_id=? AND status='ready' AND signature=?",
                   (signature(embedding,config),user_id,legacy_signature(embedding,previous)))
