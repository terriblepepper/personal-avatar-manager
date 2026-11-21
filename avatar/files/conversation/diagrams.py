"""用户/会话隔离的 Mermaid 图像缓存与源码永久转存；不执行或直接嵌入 SVG。"""
import asyncio
import base64
import hashlib
import io
import uuid
from PIL import Image, UnidentifiedImageError
from fastapi import HTTPException
from avatar.core.db import now

VERSION='mermaid-11.17.2-cache-1'
MAX_IMAGE=4*1024*1024
SOURCE_FILENAME='Mermaid绘图.mmd'


# 缓存只授权本人 Web 父会话；SVG 是不可信文本，恢复时仍进入隔离页重新清理。
class DiagramCache:
    def __init__(self,sessions):
        self.database=sessions.database
        self.attachments=sessions.attachments
        self.sessions=sessions

    def key(self,source):
        return hashlib.sha256((VERSION+'\n'+source).encode()).hexdigest()

    # 按源码精确命中；PNG 用于立即显示，SVG 只交给沙箱，不提供可执行的直出路由。
    def lookup(self,user_id,conversation_id,source):
        with self.database.connect() as db:
            self.sessions.session(db,user_id,conversation_id)
            row=db.execute("SELECT * FROM conversation_diagrams WHERE user_id=? AND conversation_id=? AND cache_key=? AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",(user_id,conversation_id,self.key(source))).fetchone()
        if not row:return {'hit':False,'version':VERSION}
        return {'hit':True,'version':VERSION,'id':row['id'],'svg':row['svg'],'png':'data:image/png;base64,'+base64.b64encode(row['png']).decode(),
                'blocked':row['blocked'],'height':row['height'],'promoted_id':row['promoted_id']}

    # 校验 PNG 并重编码，清除图片元数据；图库不接受 SVG 冒充 PNG 或超大像素内容。
    def png(self,value):
        try:
            if not value.startswith('data:image/png;base64,'):raise ValueError()
            data=base64.b64decode(value.split(',',1)[1],validate=True)
            if len(data)>MAX_IMAGE:raise ValueError()
            with Image.open(io.BytesIO(data)) as image:
                if image.format!='PNG' or image.width*image.height>16_000_000:raise ValueError()
                output=io.BytesIO();image.convert('RGBA').save(output,format='PNG')
            data=output.getvalue()
            if len(data)>MAX_IMAGE:raise ValueError()
            return data
        except (ValueError,OSError,UnidentifiedImageError,Image.DecompressionBombError):
            raise HTTPException(422,'图表缓存必须是有效 PNG，最多 4 MiB/1600 万像素') from None

    # 限额内同源码覆盖（如外部资源已加载），不重复占用空间，不把成功转存指向另一张图。
    def save(self,user_id,conversation_id,body):
        with self.database.connect() as db:self.sessions.session(db,user_id,conversation_id)
        if len(body.svg.encode())>MAX_IMAGE:raise HTTPException(413,'图表 SVG 超过 4 MiB')
        png=self.png(body.png);size=len(png)+len(body.svg.encode())+len(body.source.encode());key=self.key(body.source)
        with self.database.connect(write=True) as db:
            self.sessions.session(db,user_id,conversation_id)
            db.execute("DELETE FROM conversation_diagrams WHERE user_id=? AND conversation_id=? AND cache_key=? AND julianday(expires_at)<=julianday('now')",(user_id,conversation_id,key))
            existing=db.execute('SELECT * FROM conversation_diagrams WHERE user_id=? AND conversation_id=? AND cache_key=?',(user_id,conversation_id,key)).fetchone()
            old_size=len(existing['png'])+len(existing['svg'].encode())+len(existing['source'].encode()) if existing else 0
            count,total=db.execute('SELECT COUNT(*),coalesce(sum(octet_length(png)+octet_length(svg)+octet_length(source)),0) FROM conversation_diagrams WHERE conversation_id=?',(conversation_id,)).fetchone()
            user_total=db.execute('SELECT coalesce(sum(octet_length(png)+octet_length(svg)+octet_length(source)),0) FROM conversation_diagrams WHERE user_id=?',(user_id,)).fetchone()[0]
            if (not existing and count>=100) or total-old_size+size>50*1024*1024 or user_total-old_size+size>100*1024*1024:
                raise HTTPException(413,'绘图缓存已满：每会话 100 幅/50 MiB，每账户 100 MiB，请清理会话')
            identifier=existing['id'] if existing else str(uuid.uuid4());stamp=now()
            db.execute('''INSERT INTO conversation_diagrams(id,user_id,conversation_id,cache_key,source,svg,png,blocked,height,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,conversation_id,cache_key) DO UPDATE SET svg=excluded.svg,png=excluded.png,
              blocked=excluded.blocked,height=excluded.height,updated_at=excluded.updated_at''',
              (identifier,user_id,conversation_id,key,body.source,body.svg,png,body.blocked,body.height,stamp,stamp))
        return {'id':identifier,'version':VERSION}

    # 转存只保存 UTF-8 源码，重复操作返回已有的源码资料。
    async def promote_source(self,user,conversation_id,source,caption,visibility,new_name=None):
        from avatar.files.library.folders import renamed_filename
        filename=SOURCE_FILENAME if new_name is None else renamed_filename(SOURCE_FILENAME,new_name)
        if not source.strip():raise HTTPException(422,'Mermaid 源码不能为空')
        data=source.encode('utf-8')
        digest=hashlib.sha256(data).hexdigest()
        async with self.sessions.promotion_locks.setdefault(user['id'],asyncio.Lock()):
            def find_existing():
                with self.database.connect() as db:
                    self.sessions.session(db,user['id'],conversation_id)
                    return db.execute(
                        "SELECT id,filename FROM attachments WHERE user_id=? AND sha256=? AND content_type='text/x-mermaid' AND saved_at IS NOT NULL LIMIT 1",
                        (user['id'],digest)).fetchone()
            existing=await asyncio.to_thread(find_existing)
            if existing:
                if new_name is not None and existing['filename']!=filename:
                    raise HTTPException(409,'相同 Mermaid 源码已在资料库，可在资料页重命名')
                return {'attachment_id':existing['id'],'already_exists':True}
            created=await self.attachments.upload(user,None,filename,data,caption,deduplicate=True,
                visibility=visibility,check_name=new_name is not None)
        return {'attachment_id':created['id']}

    # 旧客户端仍可凭已授权的缓存 ID 转存缓存内的源码。
    async def promote(self,user,conversation_id,identifier,caption,visibility,new_name=None):
        def read_source():
            with self.database.connect() as db:
                self.sessions.session(db,user['id'],conversation_id)
                return db.execute("SELECT source FROM conversation_diagrams WHERE id=? AND user_id=? AND conversation_id=? AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))",
                               (identifier,user['id'],conversation_id)).fetchone()
        row=await asyncio.to_thread(read_source)
        if not row:raise HTTPException(404,'绘图缓存不存在')
        result=await self.promote_source(user,conversation_id,row['source'],caption,visibility,new_name)
        def mark_promoted():
            with self.database.connect(write=True) as db:
                db.execute('UPDATE conversation_diagrams SET promoted_id=? WHERE id=? AND user_id=? AND conversation_id=?',
                           (result['attachment_id'],identifier,user['id'],conversation_id))
        await asyncio.to_thread(mark_promoted)
        return result
