# 模块说明：会话资源引用及生成文件落盘；复用会话文件表、下载接口和删除事务。
from contextlib import contextmanager
import os
import hashlib
import json
from pathlib import Path, PurePosixPath
import uuid
from fastapi import HTTPException
from avatar.core.db import now
from avatar.files.uploads.validation import TYPES, chat_file_limit
from avatar.core.artifact_limits import MAX_SOURCE_BYTES
from avatar.files.uploads.buffer import upload_buffer, upload_work
from avatar.files.word_pdfs import WORD_SUFFIXES, link_word_pdf, prepare_word_pdf

OUTPUT_TYPES = {**TYPES, '.zip': 'application/zip'}


def original_hashes(path, size, destination=None):
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    remaining = size
    with path.open('rb') as source:
        if os.fstat(source.fileno()).st_size != size:
            raise HTTPException(409,'会话原件大小已改变')
        while remaining:
            chunk = source.read(min(remaining,1024*1024))
            if not chunk:
                raise HTTPException(409,'会话原件不完整')
            remaining -= len(chunk)
            sha.update(chunk)
            md5.update(chunk)
            if destination is not None:
                destination.write(chunk)
        if source.read(1):
            raise HTTPException(409,'会话原件大小已改变')
    return sha.hexdigest(), md5.hexdigest()


def reference_count(db, conversation_id):
    return db.execute("""SELECT count(*) FROM chat_files
        WHERE conversation_id=? AND referenced=1 AND origin!='attachment'
        AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP)""",(conversation_id,)).fetchone()[0]


# 职责：校验沙箱回传的新文件，路径只用于命名，宿主保存路径始终由服务端 UUID 决定。
def validate_output(filename,data):
    if (not isinstance(filename,str) or not filename or len(filename)>200 or PurePosixPath(filename).name!=filename
            or filename in ('.','..') or any(ord(c)<32 for c in filename) or '\\' in filename or ':' in filename
            or Path(filename).suffix.lower() not in OUTPUT_TYPES):
        raise ValueError('生成文件名或格式不支持')
    limit = chat_file_limit(filename)
    if not isinstance(data,bytes) or len(data)>limit:
        raise ValueError('生成文件超过大小限制')
    return OUTPUT_TYPES[Path(filename).suffix.lower()]


# 职责：作为 ChatFiles 的资源扩展，所有文件引用均生成独立会话副本，不授予跨账户链接。
class ConversationResources:
    # 引用目录只包含长期资料；会话临时文件须先转存为独立资料。
    def library(self,user_id,conversation_id,search='',offset=0,folder='',recursive=False):
        from avatar.files.library.folders import validate_path
        validate_path(folder,root=True)
        with self.database.connect() as db:
            self.session(db,user_id,conversation_id)
            rows=[dict(row) for row in db.execute("""SELECT a.id,a.filename,a.relative_path,a.sha256,a.content_type,a.animation_metadata,a.size,a.caption AS description,a.created_at,'profile' AS source,
                     EXISTS(SELECT 1 FROM chat_files f WHERE f.conversation_id=? AND f.sha256=a.sha256 AND (f.expires_at IS NULL OR avatar_utc_time(f.expires_at)>CURRENT_TIMESTAMP)) AS already_referenced
                     FROM attachments a WHERE a.user_id=? ORDER BY a.created_at DESC,a.id""",(conversation_id,user_id))]
            folders={row['path']:row['caption'] for row in db.execute('SELECT path,caption FROM material_folders WHERE user_id=? ORDER BY path',(user_id,))}
        for row in rows:
            row.update(json.loads(row.pop('animation_metadata')))
        prefix=folder+'/' if folder else ""
        files=[row for row in rows if (row['relative_path'] or row['filename']).startswith(prefix)]
        if search:
            files=[row for row in rows if search.casefold() in ' '.join([row['relative_path'] or row['filename'],row['description'],row.get('title',''),*row.get('tags',[])]).casefold()]
        elif not recursive:files=[row for row in files if '/' not in (row['relative_path'] or row['filename'])[len(prefix):]]
        directories=[] if search or recursive else [{'id':'folder:'+path,'filename':path[len(prefix):],'path':path,'source':'folder','description':folders[path],'count':sum((r['relative_path'] or r['filename']).startswith(path+'/') for r in rows),'size':sum(r['size'] for r in rows if (r['relative_path'] or r['filename']).startswith(path+'/'))} for path in folders if path.startswith(prefix) and path!=folder and '/' not in path[len(prefix):]]
        items=directories+files;limit=100 if recursive else 10
        return {'items':items[offset:offset+limit],'total':len(items),'offset':offset,'limit':limit,'folder':folder}

    # 将已保存资料复制为当前会话的独立附件，源资料与会话副本各自管理。
    async def reference(self,user,conversation_id,source,identifier,*,temporary=False):
        def source_row():
            with self.database.connect() as db:
                self.session(db,user['id'],conversation_id,editable=True)
                if source=='profile':
                    row=db.execute("SELECT * FROM attachments WHERE id=? AND user_id=?",(identifier,user['id'])).fetchone()
                    directory=self.attachments.directory
                elif source=='chat':raise HTTPException(422,'会话临时文件不属于资料库，请先转存为独立资料文件')
                else:raise HTTPException(422,'文件来源无效')
                if row:
                    from avatar.files.library.folders import with_folder_caption
                    row=with_folder_caption(db,row)
            if not row:raise HTTPException(404,'文件不存在')
            return row,directory
        row,directory=await upload_work(source_row)
        # 原件只按数据库 ID 定位；复制前验证大小及 SHA-256。
        try:
            original = directory.new(user['id'],row['id']+'.blob').open('rb')
        except FileNotFoundError:
            raise HTTPException(404,'源文件已删除') from None
        with original, upload_buffer(original) as data:
            if len(data) != row['size'] or (await upload_work(hashlib.sha256,data)).hexdigest() != row['sha256']:
                raise HTTPException(409,'源文件校验失败，请重新保存资料文件')
            return await self.upload(user,conversation_id,row['filename'],data,origin='reference',
                description=row['caption'][:2000],source_label='个人资料文件',temporary=temporary,
                html_mode=json.loads(row['animation_metadata']).get('html_mode'))

    # 职责：与成功回答共用事务保存生成文件；提交失败或取消时删除本次原件和预览。
    # 结果与约束：生成文件保留单轮处理边界，原上传文件不被覆盖。
    @contextmanager
    def storing_outputs(self,db,user_id,conversation_id,message_id,outputs):
        paths=[]
        try:
            storage_owner=self.session(db,user_id,conversation_id)['user_id']
            from avatar.files.library.public import PublicMaterials
            generated=[item for item in outputs if not item.get('public_material_id')]
            ordinary=sum(len(item['data']) for item in generated if OUTPUT_TYPES.get(Path(item['filename']).suffix.lower()) != 'text/html')
            total=sum(len(item['data']) for item in generated)
            if ordinary>20*1024*1024 or total>MAX_SOURCE_BYTES:
                raise HTTPException(413,'普通生成文件合计 20 MiB，生成文件总计 100 MiB')
            sources={item['id']:PublicMaterials.validate(db,user_id,item) for item in outputs if item.get('public_material_id')}
            for item in outputs:
                source=sources.get(item['id'])
                if source and item.get('reuse'):
                    existing=self.owned(db,user_id,conversation_id,item['id'])
                    if existing['sha256']!=source['sha256'] or existing['filename']!=source['filename']:
                        raise HTTPException(409,'已有会话副本已改变，请重试')
                    try:actual,_=original_hashes(self.attachments.directory.new(user_id,source['id']+'.blob'),source['size'])
                    except FileNotFoundError:raise HTTPException(410,'公开资料原件已删除') from None
                    if actual!=source['sha256']:raise HTTPException(409,'公开资料原件校验失败，文件未发送')
                    continue
                metadata, digest = '{}', ''
                if source:
                    mime, metadata, digest = source['content_type'], source['animation_metadata'], source['animation_digest']
                    size=source['size']
                else:
                    mime = validate_output(item['filename'], item['data'])
                    size = len(item['data'])
                    if item.get('html_mode'):
                        metadata = json.dumps({'html_mode': item['html_mode']})
                identifier=str(uuid.UUID(item['id']))
                path=self.directory.path(storage_owner,conversation_id,identifier+'.blob',create=True)
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                    paths.append(path)
                    if source:
                        original=self.attachments.directory.new(user_id,source['id']+'.blob')
                        try:sha,md5=original_hashes(original,size,stream)
                        except FileNotFoundError:raise HTTPException(410,'公开资料原件已删除') from None
                        if sha!=source['sha256']:raise HTTPException(409,'公开资料原件校验失败，文件未发送')
                    else:
                        stream.write(item['data'])
                        sha=hashlib.sha256(item['data']).hexdigest()
                extension = Path(item['filename']).suffix.lower()
                if extension in WORD_SUFFIXES:
                    if source:
                        pdf = link_word_pdf(original, path)
                    else:
                        pdf, _ = prepare_word_pdf(path, extension)
                    if pdf is not None:
                        paths.append(pdf)
                if mime.startswith('image/'):
                    from avatar.files.parsing.images import jpeg_bytes
                    encoded=jpeg_bytes(path);preview=path.with_suffix('.jpg')
                    with os.fdopen(os.open(preview,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                        paths.append(preview);stream.write(encoded)
                db.execute('''INSERT INTO chat_files(id,user_id,conversation_id,filename,content_type,size,extracted_text,
                    extraction_status,created_at,origin,expires_at,animation_metadata,animation_digest,generated_message_id,
                    referenced,source_label,sha256,md5,description)
                    VALUES(?,?,?,?,?,?,'','rag_pending',?,?,datetime('now','+7 days'),?,?,?,0,?,?,?,?)''',
                    (identifier,storage_owner,conversation_id,item['filename'],mime,size,now(),'reference' if source else 'generated',
                     metadata,digest,message_id,'公开资料原件' if source else '分身生成',sha,md5 if source else '',
                     source['caption'] if source else ''))
            yield paths
        except BaseException:
            for path in paths:path.unlink(missing_ok=True)
            raise
