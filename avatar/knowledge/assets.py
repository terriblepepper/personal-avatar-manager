"""Account-owned private Soul assets, imports and storage quotas."""
import asyncio
import uuid
from pathlib import Path

from fastapi import HTTPException
from avatar.core.db import event, now
from avatar.core.user_storage import UserBlobs
from avatar.files.uploads.buffer import recover_removed, staged_removals, upload_work, write_original, upload_buffer
from avatar.knowledge.media import MAX_ASSET, require_asset_format, validate
from avatar.files.library.attachments import MAX_STORED_SIZE
from avatar.files.library.batch import BatchRequest, select
from avatar.files.library.folders import validate_path, ensure_folders
from avatar.files.uploads.batches import audit_detail
from avatar.files.word_pdfs import discard_original
from avatar.knowledge.library import changed, check, result


class KnowledgeAssetStore:
    def __init__(self,database,chat):
        self.database,self.chat=database,chat
        self.directory=UserBlobs(database,'knowledge','knowledge_assets')
        self.upload_lock=asyncio.Semaphore(2)
        recover_removed(database,self.directory,'knowledge_assets')

    def asset(self,user_id,identifier,db=None):
        if db is None:
            with self.database.connect() as connection:return self.asset(user_id,identifier,connection)
        row=db.execute('SELECT * FROM knowledge_assets WHERE id=? AND user_id=?',(identifier,user_id)).fetchone()
        if not row:raise HTTPException(404,'知识库素材不存在')
        return dict(row)

    def file(self,user_id,identifier):
        row=self.asset(user_id,identifier)
        if not self.directory.available(user_id,row['id']):raise HTTPException(404,'素材原件已不存在')
        return self.directory.new(user_id,row['id']+'.blob'),row

    def upload_budget(self,user_id):
        with self.database.connect() as db:
            used, largest = db.execute("""SELECT coalesce(sum(size) FILTER (WHERE source_attachment_id=''),0),
                coalesce(max(size),0) FROM knowledge_assets WHERE user_id=?""",(user_id,)).fetchone()
        # Even with a full account, an exact duplicate returns the existing asset.
        return min(MAX_ASSET,max(MAX_ASSET-used,largest))

    async def upload(self,user,filename,data,caption='',relative_path='', *, source_attachment_id='',batch_id=''):
        async with self.upload_lock:
            _,mime,kind=await upload_work(validate,filename,data,max_size=MAX_STORED_SIZE if source_attachment_id else MAX_ASSET)
            return await upload_work(self.store,user,filename,data,mime,kind,caption,relative_path,source_attachment_id,batch_id)

    def store(self,user,filename,data,mime,kind,caption,relative_path='',source_attachment_id='',batch_id=''):
        if relative_path:
            validate_path(relative_path)
            if relative_path.split('/')[-1]!=filename:raise HTTPException(422,'素材路径必须保留文件名')
        if len(caption)>2000:raise HTTPException(422,'素材说明最多 2000 字')
        identifier=str(uuid.uuid4());path=self.directory.new(user['id'],identifier+'.blob')
        checksum=write_original(path,data,extension=Path(filename).suffix.lower())
        try:
            with self.database.connect(write=True,account_scope=user['id']) as db:
                previous=db.execute('SELECT * FROM knowledge_assets WHERE user_id=? AND sha256=?',(user['id'],checksum)).fetchone()
                if source_attachment_id and not db.execute('SELECT 1 FROM attachments WHERE id=? AND user_id=? AND sha256=?',(source_attachment_id,user['id'],checksum)).fetchone():
                    raise HTTPException(409,'来源资料文件已改变，请重新引用')
                if previous:
                    if source_attachment_id:
                        db.execute('UPDATE knowledge_assets SET source_attachment_id=? WHERE id=?',(source_attachment_id,previous['id']))
                    if batch_id:
                        event(db,user['id'],user['username'],'上传知识库素材',audit_detail(previous['id'],batch_id),actor_id=user['id'])
                    discard_original(path)
                    return self.asset(user['id'],previous['id'],db)
                total=db.execute("SELECT COALESCE(SUM(size),0) FROM knowledge_assets WHERE user_id=? AND source_attachment_id=''",(user['id'],)).fetchone()[0]
                if not source_attachment_id and total+len(data)>MAX_ASSET:
                    raise HTTPException(413,'Soul 上传素材总大小不能超过 10 GiB；从资料库引用不占用此额度')
                db.execute('INSERT INTO knowledge_assets(id,user_id,filename,mime,size,sha256,kind,caption,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                           (identifier,user['id'],filename,mime,len(data),checksum,kind,caption,now()))
                ensure_folders(db,user['id'],relative_path.rpartition('/')[0],table='knowledge_folders')
                db.execute('UPDATE knowledge_assets SET relative_path=?,source_attachment_id=? WHERE id=?',(relative_path or filename,source_attachment_id,identifier))
                changed(db,user['id'])
                event(db,user['id'],user['username'],'上传知识库素材',audit_detail(identifier,batch_id),actor_id=user['id'])
                return self.asset(user['id'],identifier,db)
        except BaseException:
            discard_original(path)
            raise

    async def import_material(self,user,identifier,folder=''):
        """只复制本人资料原件，目标知识库始终私有，源可见性与生命周期不变。"""
        def copy():
            path,item=self.chat.service.attachments.file_for(user,identifier)
            require_asset_format(item['filename'])
            try:
                with path.open('rb') as source,upload_buffer(source) as data:
                    _,mime,kind=validate(item['filename'],data,max_size=MAX_STORED_SIZE)
                    relative_path=(folder+'/' if folder else '')+item['filename']
                    return self.store(user,item['filename'],data,mime,kind,item.get('caption',''),relative_path,identifier)
            except OSError:
                raise HTTPException(404,'资料文件已删除，请刷新后重新选择') from None
        async with self.upload_lock:
            return await upload_work(copy)

    def edit_caption(self,user,identifier,caption):
        with self.database.connect(write=True,account_scope=user['id']) as db:
            previous=self.asset(user['id'],identifier,db)
            db.execute('UPDATE knowledge_assets SET caption=? WHERE id=? AND user_id=?',(caption,identifier,user['id']))
            changed(db,user['id'],analyse=previous['caption']!=caption)
            event(db,user['id'],user['username'],'修改知识库素材说明',actor_id=user['id'])
            return self.asset(user['id'],identifier,db)

    def delete_asset(self,user,identifier):
        return self.batch(user,BatchRequest(action='delete',ids=[identifier]))

    def batch(self,user,body):
        if body.action!='delete':raise HTTPException(422,'Soul 素材始终私有，不支持公开权限')
        with staged_removals() as stage:
            with self.database.connect(write=True,account_scope=user['id']) as db:
                check(db,user,body.last_modified_at)
                rows,folders=select(db,user['id'],body,knowledge=True)
                identifiers={row['id'] for row in rows}
                in_use={r[0] for r in db.execute("SELECT a.value FROM soul_builds b,json_each(b.asset_ids) a WHERE b.user_id=? AND b.status IN ('queued','running','waiting_user','ready','pending')",(user['id'],))}
                if identifiers & in_use:raise HTTPException(409,'所选素材正在构建或审核，请先取消对应构建；本次未删除任何条目')
                db.executemany('DELETE FROM knowledge_assets WHERE id=? AND user_id=?',[(i,user['id']) for i in identifiers])
                db.executemany('DELETE FROM knowledge_folders WHERE user_id=? AND path=?',[(user['id'],p) for p in folders])
                changed(db,user['id'],analyse=False)
                event(db,user['id'],user['username'],'批量删除 Soul 素材',str(len(identifiers)),actor_id=user['id'])
                for identifier in identifiers:
                    stage(self.directory.new(user['id'],identifier+'.blob'))
        return {**result(self,user['id']),'deleted':True,'changed_count':len(identifiers)}
