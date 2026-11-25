"""Coordinate index invalidation with the transaction that saves model settings."""
from avatar.knowledge.model_refresh import schedule
from avatar.models.embedding import index_identity
from avatar.rag.invalidation import invalidate, modalities


def model_changed(db, owner, previous, current, *, embedding=False, embedding_key_changed=False):
    if embedding:
        changed = index_identity(previous) != index_identity(current)
        visual = bool(previous.get('multimodal_enabled')) != bool(current.get('multimodal_enabled'))
        if embedding_key_changed or any(current.get(key) != previous.get(key) for key in current if key != 'encrypted_key'):
            invalidate(db, owner, force=visual)
        if changed:
            schedule(db, owner, embedding=True)
        return
    before, after = modalities(previous), modalities(current)
    if before != after:
        invalidate(db, owner)
        if before[0] != after[0]:
            schedule(db, owner, visual=True)
