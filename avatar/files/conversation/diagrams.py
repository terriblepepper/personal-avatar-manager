"""Persist authenticated Mermaid source as a material file."""
import asyncio
import hashlib

from fastapi import HTTPException

SOURCE_FILENAME = 'Mermaid绘图.mmd'


async def save_mermaid(sessions,user,conversation_id,source,caption,visibility,new_name=None):
    from avatar.files.library.folders import renamed_filename
    filename=SOURCE_FILENAME if new_name is None else renamed_filename(SOURCE_FILENAME,new_name)
    if not source.strip():raise HTTPException(422,'Mermaid 源码不能为空')
    data=source.encode('utf-8')
    digest=hashlib.sha256(data).hexdigest()
    async with sessions.promotion_locks.setdefault(user['id'],asyncio.Lock()):
        def find_existing():
            with sessions.database.connect() as db:
                sessions.session(db,user['id'],conversation_id)
                return db.execute(
                    "SELECT id,filename FROM attachments WHERE user_id=? AND sha256=? AND content_type='text/x-mermaid' AND saved_at IS NOT NULL LIMIT 1",
                    (user['id'],digest)).fetchone()
        existing=await asyncio.to_thread(find_existing)
        if existing:
            if new_name is not None and existing['filename']!=filename:
                raise HTTPException(409,'相同 Mermaid 源码已在资料库，可在资料页重命名')
            return {'attachment_id':existing['id'],'already_exists':True}
        created=await sessions.attachments.upload(user,None,filename,data,caption,deduplicate=True,
            visibility=visibility,check_name=new_name is not None)
    return {'attachment_id':created['id']}
