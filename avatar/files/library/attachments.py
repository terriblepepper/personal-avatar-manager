from avatar.core.user_storage import ConversationBlobs, UserBlobs
from avatar.files.uploads.buffer import upload_work, write_original
# 模块说明：独立资料文件存储、归属、说明和隐私；用户保存立即生效，原件不进入公开静态目录。
import asyncio
import json
import uuid
import zipfile
from io import BytesIO
from contextlib import nullcontext
from pathlib import Path
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from avatar.accounts.roles import ADMIN_ROLES
from avatar.core.db import now, dump, event
from avatar.files.artifact_limits import MAX_SOURCE_BYTES
from avatar.files.parsing.media_types import MEDIA,validate_media
from avatar.profiles.profile_dates import next_modified_at
from avatar.profiles.profile_dates import ModifiedAt

# Model and sandbox outputs retain a separate per-file processing bound.
MAX_FILE = 10 * 1024 * 1024
MAX_STORED_SIZE = 10 * 1024 ** 3
TYPES = {'.txt': 'text/plain', '.md': 'text/markdown', '.mmd': 'text/x-mermaid', '.html': 'text/html', '.htm': 'text/html', '.csv': 'text/csv', '.json': 'application/json',
         '.log': 'text/plain', '.tsv': 'text/tab-separated-values',
         '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp', '.pdf': 'application/pdf',
         '.doc': 'application/msword', '.ppt': 'application/vnd.ms-powerpoint',
         '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',**MEDIA}


ARTIFACT_SUFFIXES = ('.html', '.htm')


def chat_file_limit(filename):
    return MAX_SOURCE_BYTES if (filename or '').lower().endswith(ARTIFACT_SUFFIXES) else MAX_FILE


class OfficeDirectory:
    """Storage examines only ZIP directory metadata. Parsers bound expanded document data separately."""

    def __init__(self, stream, length):
        self.stream, self.length = stream, length
        self.seek, self.tell = stream.seek, stream.tell

    def read(self, size=-1):
        if size < 0:
            size = max(0,self.length - self.tell())
        if size > 16 * 1024**2:
            raise ValueError('Office ZIP 目录过大')
        return self.stream.read(size)


# 职责：定义个人资料中一个附件的关联与可见性。
# 输入：id 必须为服务端 UUID，visibility 默认私密，caption 为上传后填写的资料说明。
# 结果与约束：上传暂存允许空说明，保存和提交允许空说明；不接受路径、所有人和提取文字等伪造元数据。
class AttachmentRef(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    visibility: Literal['public', 'private'] = 'private'
    caption: str = Field(default='', max_length=2000)


# 职责：定义单个文件的说明与可见性保存请求。
# 输入：caption 为选填说明，updated_at 可选核对当前文件日期，last_modified_at 兼容核对文件集合日期。
# 结果与约束：只编辑本人文件，不接受文件路径或所有人；说明最多 2000 字，说明和可见性立即生效，不参与个人资料审核。
class AttachmentDescriptionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    caption: str | None = Field(default=None,max_length=2000)
    last_modified_at: ModifiedAt | None = None
    visibility: Literal['public','private'] | None = None
    updated_at: ModifiedAt | None = None


# 职责：统一管理附件原件、元数据与用户自行保存的公开投影。
# 输入：database 为应用唯一数据库，文件目录从其实际数据目录派生。
# 结果与约束：文件不可变、默认私密，说明选填，隐私保存立即生效；提取过程限并发、限大小与资源，不进行模型计费调用。
class AttachmentService:
    # 职责：配置当前数据目录下的受保护原件位置。
    # 输入：database 为 Database 实例。
    # 结果与约束：仅建立服务依赖和解析并发限制，实际写入时创建 0700 目录。
    def __init__(self, database):
        self.database = database
        self.directory = UserBlobs(database,'attachments','attachments')
        self.parsing = asyncio.Semaphore(2)
        from avatar.files.uploads.buffer import recover_removed
        recover_removed(database,self.directory,'attachments')

    # 职责：校验上传名称、扩展名和基础文件结构。
    # 输入：filename 为客户端文件名，data 为已经限长的原件字节。
    # 结果与约束：名称不参与存储路径；拒绝空文件、不支持类型、扩展名伪装及宏文件/超限压缩包。
    @staticmethod
    def inspect_file(filename, data, *, max_size=None, generated=False):
        if not filename or len(filename) > 200 or any(ord(c) < 32 for c in filename) or '/' in filename or '\\' in filename:
            raise HTTPException(422, '文件名无效')
        extension = Path(filename).suffix.lower()
        if extension not in TYPES and not (generated and extension=='.zip'):
            raise HTTPException(415, '支持文本、图片、音频、视频、CSV/TSV/XLSX、PDF、Word 和 PPT；旧 XLS 请先另存为 XLSX/CSV')
        if not data:
            raise HTTPException(422, '不能上传空文件')
        if extension in ARTIFACT_SUFFIXES and len(data) > MAX_SOURCE_BYTES:
            raise HTTPException(413, 'HTML 文件不能超过 100 MiB')
        if max_size is not None and len(data) > max_size:
            raise HTTPException(413, '文件超过上传大小限制')
        if extension in ('.zip','.docx','.pptx','.xlsx'):
            archive_source = data if hasattr(data, 'seek') else BytesIO(data)
        if extension=='.zip' and not zipfile.is_zipfile(archive_source):
            raise HTTPException(415,'ZIP 文件格式无效')
        if extension in MEDIA:validate_media(extension,data[:32])
        if extension in ('.docx', '.pptx', '.xlsx'):
            try:
                if data[:4] != b'PK\x03\x04':
                    raise ValueError('Office 文件头无效')
                with zipfile.ZipFile(OfficeDirectory(archive_source, len(data))) as archive:
                    items = archive.infolist()
                    required = {'.docx':'word/document.xml','.pptx':'ppt/presentation.xml','.xlsx':'xl/workbook.xml'}[extension]
                    if required not in archive.namelist() or len(items) > 5000:
                        raise ValueError()
                    if any('vbaproject' in item.filename.lower() for item in items):
                        raise ValueError()
            except (zipfile.BadZipFile, ValueError):
                raise HTTPException(415, 'Office 文件格式无效、包含宏或目录超限，请使用普通 DOCX/PPTX')
        signatures = {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8', '.jpeg': b'\xff\xd8',
                      '.gif': b'GIF8', '.bmp': b'BM', '.doc': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',
                      '.ppt': b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'}
        if extension in signatures and data[:len(signatures[extension])] != signatures[extension]:
            raise HTTPException(415, '文件内容与扩展名不匹配')
        if extension == '.webp' and not (data[:4] == b'RIFF' and data[8:12] == b'WEBP'):
            raise HTTPException(415, 'WebP 文件格式无效')
        if extension in ('.txt', '.md', '.mmd', '.csv', '.tsv', '.json', '.log', '.html', '.htm') and data.find(b'\x00') >= 0 and not data[:4].startswith((b'\xff\xfe',b'\xfe\xff',b'\x00\x00\xfe\xff')):
            raise HTTPException(415, '文本文件包含二进制内容')
        return extension

    # 职责：读取账户文件集合的独立修改日期。
    # 输入：user_id 为已授权文件所有人。
    # 结果与约束：无文件操作返回空字符串，与个人资料日期和审核状态独立。
    def collection_date(self, user_id, db=None):
        with (nullcontext(db) if db is not None else self.database.connect()) as connection:
            row=connection.execute('SELECT updated_at FROM attachment_collections WHERE user_id=?',(user_id,)).fetchone()
        return row['updated_at'] if row else ''

    # 职责：在事务中推进文件集合日期，使旧文件记忆不再参与后续回答。
    # 输入：db 为写事务，user_id 为所有人。
    # 结果与约束：日期严格递增且晚于生效资料日期，不改变资料草稿、审核状态或资料日期。
    @staticmethod
    def touch(db, user_id):
        row=db.execute('SELECT updated_at FROM attachment_collections WHERE user_id=?',(user_id,)).fetchone()
        agent=db.execute('SELECT last_modified_at FROM avatar_records WHERE user_id=?',(user_id,)).fetchone()
        modified=next_modified_at(max(row['updated_at'] if row else '',agent['last_modified_at'] if agent else ''))
        db.execute('INSERT INTO attachment_collections(user_id,updated_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at',(user_id,modified))
        return modified

    # 职责：核对账户文件集合的并发日期，不检查个人资料审核状态。
    # 输入：db 为事务，user_id 来自认证，last_modified_at 可选代表此前文件集合日期。
    # 结果与约束：资料待审核或执行时仍可管理文件；过期集合日期返回 409，不接受他人账号。
    @staticmethod
    def editable(db, user_id, last_modified_at=None):
        row=db.execute('SELECT updated_at FROM attachment_collections WHERE user_id=?',(user_id,)).fetchone()
        if last_modified_at is not None and (row['updated_at'] if row else '')!=last_modified_at:
            raise HTTPException(409,'资料文件已变更，请刷新后再操作')
        return row

    @staticmethod
    def quota(db,user_id):
        total = db.execute('SELECT coalesce(sum(size),0) FROM attachments WHERE user_id=?',(user_id,)).fetchone()[0]
        return {'used_bytes': total, 'limit_bytes': MAX_STORED_SIZE}

    # 职责：接收有界原件并在隔离线程中解析入库。
    # 输入：user 为已认证账户，last_modified_at 可选核对文件集合日期，filename 与 data 为限长文件，caption 可留空后续填写。
    # 结果与约束：上传说明最多 2000 字符但允许空；说明选填，不影响索引或引用，每进程最多两个解析任务，未保存文件可立即删除。
    async def upload(self, user, last_modified_at, filename, data, caption='', *, relative_path='', deduplicate=False, batch_id='', generated=False, visibility=None, check_name=False):
        caption = caption.strip()
        if len(caption) > 2000:
            raise HTTPException(422, '资料说明最多 2000 字')
        if relative_path:
            parts=relative_path.split('/')
            if (len(relative_path)>1024 or len(parts)>21 or relative_path.startswith('/') or '\\' in relative_path
                    or any(':' in part for part in parts[:-1])
                    or any(part in ('','.','..') or any(ord(c)<32 or ord(c)==127 for c in part) for part in parts)
                    or parts[-1]!=filename):
                raise HTTPException(422,'文件夹路径无效，请使用相对目录且保留原文件名')
        async with self.parsing:
            extension = await upload_work(self.inspect_file, filename, data, max_size=MAX_STORED_SIZE, generated=generated)
            return await upload_work(self._store, user, last_modified_at, filename, data, extension, caption, relative_path, deduplicate, batch_id, visibility, check_name)

    # 职责：保存不可变原件并提交元数据，派生索引由后台更新。
    # 输入：校验后的用户、修改日期、名称、字节、扩展名及可为空的暂存说明。
    # 结果与约束：写入 0600 UUID 文件，最终事务重新检查日期与配额；失败删除未登记原件，解析失败仍保留文件供补充说明。
    def _store(self, user, last_modified_at, filename, data, extension, caption, relative_path='', deduplicate=False, batch_id='', visibility=None, check_name=False):
        with self.database.connect() as db:
            self.editable(db, user['id'], last_modified_at)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        identifier = str(uuid.uuid4())
        path = self.directory.new(user['id'],identifier + '.blob')
        checksum = write_original(path,data)
        try:
            extracted,status='','rag_pending'
            with self.database.connect(write=True) as db:
                self.editable(db,user['id'],last_modified_at)
                if deduplicate and db.execute('SELECT 1 FROM attachments WHERE user_id=? AND sha256=?',(user['id'],checksum)).fetchone():raise HTTPException(409,'相同内容已在资料库中，不能重复转存')
                quota = self.quota(db,user['id'])
                if quota['used_bytes']+len(data)>quota['limit_bytes']:
                    raise HTTPException(413,'资料文件总大小不能超过 10 GiB，请先删除不需要的文件')
                from avatar.files.library.folders import ensure_folders,ensure_filename_available
                if check_name:ensure_filename_available(db,user['id'],relative_path or filename)
                ensure_folders(db,user['id'],'/'.join(relative_path.split('/')[:-1]))
                modified=self.touch(db,user['id'])
                from avatar.files.library.batch import inherited
                effective_visibility=visibility or inherited(db,user['id'],relative_path.rpartition('/')[0])
                db.execute('INSERT INTO attachments(id,user_id,filename,content_type,size,sha256,extracted_text,extraction_status,created_at,caption,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier,user['id'],filename,'application/zip' if extension=='.zip' else TYPES[extension],len(data),checksum,extracted,status,now(),caption,modified))
                db.execute('UPDATE attachments SET extraction_version=1,relative_path=?,visibility=?,visibility_override=?,saved_at=? WHERE id=?',
                           (relative_path,effective_visibility,int(visibility is not None),modified if visibility is not None else None,identifier))
                from avatar.files.uploads.batches import audit_detail
                event(db, user['id'], user['username'], '上传资料附件', audit_detail(identifier,batch_id), actor_id=user['id'])
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        ref = {'id': identifier, 'visibility': effective_visibility, 'caption': caption}
        return {**self.metadata(user['id'], [ref])[0], 'last_modified_at': modified}

    # 职责：将引用解析为本人或管理员可查看的附件摘要。
    # 输入：user_id 固定所有人，references 来自独立文件清单或只读旧审核记录，admin 控制下载路由。
    # 结果与约束：返回有限文字预览，不暴露磁盘路径；私密附件只用于本人/管理员界面，不用于公开模型背景。
    def metadata(self, user_id, references, admin=False):
        references = list(references or ())
        if not references:
            return []
        with self.database.connect() as db:
            rows = db.execute('SELECT '+self.summary_fields+' FROM attachments WHERE user_id=? AND id=ANY(?::text[])',
                              (user_id, list({ref['id'] for ref in references})))
            by_id = {row['id']: self._summary(row, admin) for row in rows}
        return [{**by_id[ref['id']], 'visibility': ref.get('visibility','private'), 'caption': ref.get('caption','')}
                for ref in references if ref['id'] in by_id]

    summary_fields = """id,filename,size,content_type,relative_path,created_at,extraction_status,
        substr(extracted_text,1,1000) AS text_preview,visibility,caption,visibility_override,
        saved_at,updated_at,animation_metadata"""

    @staticmethod
    def _summary(row, admin=False):
        item = dict(row)
        item.update(json.loads(item.pop('animation_metadata')))
        item.update(relative_path=item['relative_path'] or item['filename'],
                    visibility_override=bool(item['visibility_override']), lifecycle='saved' if item['saved_at'] else 'staged',
                    download_url=('/api/admin/attachments/' if admin else '/api/me/attachments/') + item['id'])
        return item

    def public_context(self,user_id,references=None,question='',db=None,*,target_aic=None,include_private=False):
        with (nullcontext(db) if db is not None else self.database.connect()) as connection:
            if target_aic is not None and not connection.execute('SELECT 1 FROM avatar_records WHERE aic=? AND user_id=?',(target_aic,user_id)).fetchone():raise HTTPException(404,'目标分身不存在')
            return [{'id':row['id'],'filename':row['filename'],'caption':row['caption'][:300],'text_excerpt':'','excerpt_only':True} for row in connection.execute("SELECT id,filename,caption FROM attachments WHERE user_id=? AND (visibility='public' OR ?=1) ORDER BY created_at DESC",(user_id,int(include_private)))]

    # 职责：列出本人独立管理的所有文件。
    # 输入：user_id 为当前认证账户，文件设置来自附件表而非审核快照。
    # 结果与约束：包含暂存和已保存文件、说明、可见性和并发日期，不暴露路径或他人内容。
    def inventory(self, user_id, admin=False):
        with self.database.connect() as db:
            return [self._summary(row,admin) for row in db.execute(
                'SELECT '+self.summary_fields+' FROM attachments WHERE user_id=? ORDER BY created_at DESC,id',(user_id,))]

    # 职责：保存单文件说明和公开性，立即供后续检索或下载使用。
    # 输入：user 为所有人，identifier 为文件 ID，request 含选填说明、可选可见性及文件并发日期。
    # 结果与约束：无需审核，空说明允许保存；只修改独立文件设置，不改变个人资料日期或申请状态；私密设置立即撤销公开访问并排除旧记忆。
    def update_description(self, user, identifier, request):
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT * FROM attachments WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
            if not row:
                raise HTTPException(404,'附件不存在')
            if request.updated_at is not None and request.updated_at!=row['updated_at']:
                raise HTTPException(409,'文件设置已变更，请刷新后再保存')
            self.editable(db,user['id'],request.last_modified_at)
            modified=self.touch(db,user['id'])
            db.execute('UPDATE attachments SET caption=?,visibility=?,visibility_override=?,saved_at=coalesce(saved_at,?),updated_at=? WHERE id=?',(row['caption'] if request.caption is None else request.caption,request.visibility or row['visibility'],int(request.visibility is not None or row['visibility_override']),modified,modified,identifier))
            event(db,user['id'],user['username'],'保存资料文件设置（立即生效）',identifier, actor_id=user['id'])
        return {'last_modified_at':modified,'files':self.inventory(user['id'])}

    # 职责：授权本人或管理员读取不可变文件原件。
    # 输入：user 为当前账户，identifier 为文件 ID，admin 仅由管理权限路由设为 True。
    # 结果与约束：普通路径不能读取他人文件，返回 404 避免枚举；原件始终作为下载附件响应。
    def file_for(self, user, identifier, admin=False):
        with self.database.connect() as db:
            row = db.execute('SELECT * FROM attachments WHERE id=?', (identifier,)).fetchone()
        if not row or (row['user_id'] != user['id'] and not (admin and user['role'] in ADMIN_ROLES)):
            raise HTTPException(404, '附件不存在')
        path = self.directory / (row['id'] + '.blob')
        if not path.is_file():
            raise HTTPException(404, '附件原件缺失，请联系管理员恢复备份')
        return path, dict(row)

    # 职责：确认公开下载链接仍对应所有人当前已保存公开文件。
    # 输入：aic 为已生效分身身份，identifier 为具体附件 ID。
    # 结果与约束：暂存文件不开放下载；独立保存为私密或删除后旧公开链接立即失效；不依赖仅凭随机 ID 的公开访问。
    def public_file(self, aic, identifier):
        with self.database.connect() as db:
            agent = db.execute('SELECT user_id,profile FROM avatar_records WHERE aic=?', (aic,)).fetchone()
        with self.database.connect() as db:
            public=db.execute("SELECT 1 FROM attachments WHERE id=? AND user_id=? AND visibility='public'",(identifier,agent['user_id'] if agent else '')).fetchone()
        if not agent or not public:
            raise HTTPException(404,'公开附件不存在')
        return self.file_for({'id': agent['user_id'], 'role': 'user'}, identifier)

    # 职责：彻底删除本人文件及所有资料/审核快照中的引用和说明。
    # 输入：user 为所有人，identifier 为文件 ID，last_modified_at 可选核对此前文件集合日期。
    # 结果与约束：历史文件也立即物理删除；撤销公开链接并推进独立文件日期排除旧记忆，删除失败回滚引用和原件，其他资料及聊天原文保留。
    def remove(self,user,identifier=None,last_modified_at=None,*,folder_path=None,recursive=False,selection=None):
        originals=[]
        reference_paths=[]
        try:
            with self.database.connect(write=True) as db:
                self.editable(db,user['id'],last_modified_at)
                if selection is not None:
                    from avatar.files.library.batch import select
                    rows,folders=select(db,user['id'],selection)
                    db.executemany('DELETE FROM material_folders WHERE user_id=? AND path=?',[(user['id'],p) for p in folders])
                elif folder_path is None:
                    rows=db.execute('SELECT id FROM attachments WHERE id=? AND user_id=?',(identifier,user['id'])).fetchall()
                    if not rows:raise HTTPException(404,'附件不存在')
                else:
                    from avatar.files.library.folders import validate_path
                    validate_path(folder_path)
                    if not db.execute('SELECT 1 FROM material_folders WHERE user_id=? AND path=?',(user['id'],folder_path)).fetchone():raise HTTPException(404,'文件夹不存在')
                    prefix=folder_path+'/'
                    rows=db.execute('SELECT id FROM attachments WHERE user_id=? AND substr(relative_path,1,?)=?',(user['id'],len(prefix),prefix)).fetchall()
                    children=db.execute('SELECT 1 FROM material_folders WHERE user_id=? AND substr(path,1,?)=?',(user['id'],len(prefix),prefix)).fetchone()
                    if (rows or children) and not recursive:raise HTTPException(409,'文件夹非空，请确认删除全部文件与子目录')
                    db.execute('DELETE FROM material_folders WHERE user_id=? AND (path=? OR substr(path,1,?)=?)',(user['id'],folder_path,len(prefix),prefix))
                identifiers={row['id'] for row in rows}
                modified=self.touch(db,user['id'])
                profile=db.execute('SELECT draft FROM profile_records WHERE user_id=?',(user['id'],)).fetchone()
                draft=json.loads(profile['draft']) if profile else {}
                if any(ref['id'] in identifiers for ref in draft.get('attachments',[])):
                    draft['attachments']=[ref for ref in draft['attachments'] if ref['id'] not in identifiers]
                    db.execute('UPDATE profile_records SET draft=? WHERE user_id=?',(dump(draft),user['id']))
                for snapshot in db.execute('SELECT id,snapshot FROM profile_submissions WHERE user_id=?',(user['id'],)).fetchall():
                    content=json.loads(snapshot['snapshot'])
                    if any(ref['id'] in identifiers for ref in content.get('attachments',[])):
                        content['attachments']=[ref for ref in content['attachments'] if ref['id'] not in identifiers]
                        db.execute('UPDATE profile_submissions SET snapshot=? WHERE id=?',(dump(content),snapshot['id']))
                agent=db.execute('SELECT profile FROM avatar_records WHERE user_id=?',(user['id'],)).fetchone()
                if agent:
                    content=json.loads(agent['profile'])
                    if any(ref['id'] in identifiers for ref in content.get('attachments',[])):
                        content['attachments']=[ref for ref in content['attachments'] if ref['id'] not in identifiers]
                        db.execute('UPDATE avatar_records SET profile=? WHERE user_id=?',(dump(content),user['id']))
                if identifiers:
                    chat_directory = ConversationBlobs(self.database)
                    reference_paths = [chat_directory.path(user['id'],r['conversation_id'],r['id']+'.blob') for r in db.execute(
                        'SELECT id,conversation_id FROM chat_files WHERE user_id=? AND source_animation_id=ANY(?::text[])',
                        (user['id'],list(identifiers)))]
                db.executemany('DELETE FROM attachments WHERE id=? AND user_id=?',[(item,user['id']) for item in identifiers])
                event(db,user['id'],user['username'],'删除资料文件夹及全部内容' if folder_path is not None else '删除资料文件及全部说明',folder_path if folder_path is not None else identifier or str(len(identifiers)),actor_id=user['id'])
                # 原件只在磁盘暂存；提交失败原路恢复，避免大文件回滚占用内存。
                for item in sorted(identifiers):
                    path=self.directory/(item+'.blob')
                    if path.exists():
                        staged=path.with_name('.removed-'+item+'-'+uuid.uuid4().hex)
                        path.replace(staged);originals.append((path,staged))
        except BaseException:
            for path,staged in originals:staged.replace(path)
            raise
        from avatar.files.uploads.buffer import clean_removed
        clean_removed(originals)
        clean_removed((None,path) for path in reference_paths)
        return {'changed_count':len(identifiers),'retained':False,'deleted':True,'last_modified_at':modified,'updated_at':modified}
