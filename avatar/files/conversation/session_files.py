"""Read originals granted by this conversation and bind receipts to their current source."""
import codecs
import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path

from fastapi import HTTPException

from avatar.core.execution import authorization
from avatar.files.parsing import PLAIN_TEXT_EXTENSIONS
from avatar.files.parsing.animation import MAX_SOURCE_BYTES


_CONTROL = re.compile(r'[\x00-\x08\x0e-\x1f\x7f-\x9f]')


def usable_text(text):
    return bool(text and not _CONTROL.search(text) and any(ch.isprintable() and not ch.isspace() for ch in text))


class SessionFiles:
    def __init__(self, files, owner, conversation, proofs, *, grants=None, sandbox_ids=()):
        self.files, self.owner, self.conversation = files, owner, conversation
        self.grants, self.sandbox_ids = grants, set(sandbox_ids)
        self.proofs = {key:{'source':'chat', 'origin':'generated', **proof} for key,proof in proofs.items()}
        self.verified = {}

    def _rows(self, db, ids, *, lock=False):
        self.files.session(db, self.owner, self.conversation)
        slots = ','.join('?' for _ in ids)
        rows = db.execute(f"""SELECT id,filename,sha256,size,content_type,animation_metadata,origin,'chat' AS source
            FROM chat_files WHERE id IN ({slots}) AND user_id=? AND conversation_id=?
            AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))"""
            + (' ORDER BY id FOR SHARE' if lock else ''), (*ids, self.owner, self.conversation))
        result = {row['id']: dict(row) for row in rows}
        if self.grants is not None:
            granted = self.grants(db)
            return {key:row for key,row in result.items() if key in granted and row['sha256']==granted[key]}
        # One locked link suffices to keep the exact attachment shared until the answer commits.
        link_lock = ' FOR SHARE OF l,m' if lock else ''
        rows = db.execute(f"""SELECT a.id,a.filename,a.sha256,a.size,a.content_type,a.animation_metadata,
            'material' AS origin,'profile' AS source FROM attachments a
            WHERE a.id IN ({slots}) AND a.user_id=? AND EXISTS(
                SELECT 1 FROM chat_message_materials l JOIN chat_messages m ON m.id=l.message_id
                WHERE l.attachment_id=a.id AND m.user_id=? AND m.conversation_id=?{link_lock})"""
            + (' ORDER BY a.id FOR SHARE OF a' if lock else ''), (*ids,self.owner,self.owner,self.conversation))
        for row in rows:
            if row['id'] in result:
                raise HTTPException(409, '当前会话存在重复文件 ID，请重新添加文件')
            result[row['id']]=dict(row)
        return result

    def _directory(self, row):
        return self.files.attachments.directory if row['source']=='profile' else self.files.directory

    def _original(self, row):
        identifier = row['id']
        path = self._directory(row).new(self.owner, identifier+'.blob')
        try:
            info = path.lstat()
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns)
            if info.st_size != row['size'] or not stat.S_ISREG(info.st_mode):
                raise HTTPException(409, '会话文件原件已改变')
            cached = self.verified.get(identifier)
            if not cached or cached[0] != stamp:
                with path.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                cached = self.verified[identifier] = (stamp,digest)
            if row['sha256'] and row['sha256'] != cached[1]:
                raise HTTPException(409, '会话文件原件校验失败')
            row['sha256'] = cached[1]
        except FileNotFoundError:
            raise HTTPException(410, '会话文件原件已删除或过期') from None
        return path

    def _referenced(self, db, identifiers):
        slots = ','.join('?' for _ in identifiers)
        return {row['id'] for row in db.execute(f"""SELECT id FROM chat_files
            WHERE user_id=? AND conversation_id=? AND referenced=1 AND id IN ({slots})""",
            (self.owner,self.conversation,*identifiers))}

    def validate(self):
        if self.proofs:
            with self.files.database.connect() as db:
                self.validate_in(db)

    def validate_in(self, db, *, lock=False):
        if not self.proofs:
            return
        rows = self._rows(db, tuple(self.proofs), lock=lock)
        explicit = {i for i,p in self.proofs.items() if p.get('explicit_reference')}
        if explicit and self._referenced(db,tuple(explicit))!=explicit:
            raise HTTPException(409,'本轮明确授权的文件引用已撤回')
        for identifier, proof in self.proofs.items():
            row = rows.get(identifier)
            if row is not None and proof.get('listed_only'):
                if not self._directory(row).available(self.owner,identifier,row['size']):
                    row = None
            elif row is not None:
                self._original(row)
            if row is None or any(proof.get(key) != value for key, value in row.items()):
                if proof.get('listed_only'):
                    raise HTTPException(409, '原任务目录已变化，请结束原任务后重新发送，以获取当前可用文件')
                raise HTTPException(409, '此前读取的会话文件已过期、删除、改变或失去授权')

    def discover(self, query, offset, outputs=()):
        result = self.files.discover(self.owner,self.conversation,query,offset,outputs,grants=self.grants)
        ids = [item['id'] for item in result['items'] if item['can_read']]
        if ids:
            with self.files.database.connect() as db:
                rows = self._rows(db, ids)
            visible=[]
            for item in result['items']:
                if item['can_read']:
                    row = rows.get(item['id'])
                    if row is None or not self._directory(row).available(self.owner,row['id'],row['size']):
                        continue
                    self.proofs.setdefault(row['id'],{**row,'listed_only':True,'read_content':False})
                visible.append(item)
            result['items']=visible
        return {'kind':'directory','has_content':False,**result}

    def read(self, identifier, offset, page, *, sandbox, explicit=False, limit=None, vision=False):
        with self.files.database.connect() as db:
            row = self._rows(db, (identifier,)).get(identifier)
            if explicit and (not row or identifier not in self._referenced(db,(identifier,))):
                raise HTTPException(409,'本轮明确授权的文件已删除、过期或撤回')
        unavailable = '当前会话找不到此已交付且获准读取的文件，请核对目录中的 ID 和 can_read'
        if not row:
            raise ValueError(unavailable)
        try:
            path = self._original(row)
        except HTTPException as exc:
            if exc.status_code in (404,410):
                if explicit:
                    raise HTTPException(410,'本轮明确授权的原件已删除或过期') from None
                raise ValueError(unavailable) from None
            raise
        previous = self.proofs.get(identifier)
        if previous and any(previous.get(k)!=v for k,v in row.items()
                            if k!='sha256' or previous.get('sha256') or not previous.get('listed_only')):
            raise HTTPException(409, '此前读取的会话文件已改变')
        result = {k:row[k] for k in ('id','filename','sha256','size','content_type','source','origin')}
        result['kind']='file'
        extension = Path(row['filename']).suffix.lower()
        if extension in PLAIN_TEXT_EXTENSIONS or extension in ('.htm','.html') or row['content_type'].startswith('text/'):
            if page != 1 or (offset > 0 and offset >= row['size']):
                raise ValueError('文本按 UTF-8 字节偏移读取，请使用返回的 next_offset')
            with path.open('rb') as stream:
                stream.seek(offset)
                chunk = stream.read(limit if limit is not None else 8192)
            try:
                decoder = codecs.getincrementaldecoder('utf-8')()
                text = decoder.decode(chunk, final=offset+len(chunk)==row['size'])
            except UnicodeDecodeError:
                if offset:
                    raise ValueError('UTF-8 字节偏移无效，请使用上页返回的 next_offset') from None
                result.update(coverage='metadata_only', parse_error='原件未使用 UTF-8 编码')
            else:
                end = offset + len(chunk) - len(decoder.getstate()[0])
                result.update(text=text, offset=offset, next_offset=end if end<row['size'] else None)
        elif extension in ('.pdf','.doc','.docx','.ppt','.pptx','.xlsx','.avanim'):
            from avatar.files.parsing.text_worker import read_original
            try:
                parsed = read_original(path,extension,start=page,end=None if explicit else page,offset=offset,
                                       limit=max(1,limit//4) if limit is not None and not explicit else limit or 2000)
            except (subprocess.SubprocessError, OSError, ValueError):
                parsed = {'error':'文档正文解析失败或超时'}
            if parsed.get('error'):
                result.update(coverage='metadata_only', parse_error=parsed['error'])
            else:
                result.update(parsed)
                if not explicit and not parsed.get('next') and page<parsed['total_pages']:
                    result.update(next={'start':page+1,'offset':0}, complete=False)
        else:
            result.update(coverage='metadata_only')
        if 'text' in result and not usable_text(result['text']):
            result.pop('text')
            result.update(coverage='metadata_only', parse_error='此段为空白或含非文本控制字符')
        for part in result.get('pages', ()):
            if not usable_text(part.get('text')):
                part['text']=''
        result['has_content'] = bool(result.get('text') or any(part['text'] for part in result.get('pages', ())))
        if not result['has_content']:
            result['coverage']='metadata_only'
        if result.get('coverage')=='metadata_only' or result.get('empty_pages'):
            scope = authorization()
            permitted = (identifier in self.sandbox_ids or (self.grants is None and (
                row['origin']=='generated' or row['source']=='profile' and
                (scope is None or identifier in scope.get('materials',())))))
            if not permitted:
                result['sandbox'] = '原件尚未获准进入沙箱。请先将该文件设为会话引用。'
            elif row['size'] > 8*1024*1024:
                result['sandbox'] = '此原件超过沙箱 8 MiB 输入上限。'
            else:
                result['sandbox'] = ('可用本 ID 作为 Python 沙箱 input_ids 处理原件。' if sandbox else
                                     '如需处理原始二进制，请先开启本会话 Python 沙箱。')
        if vision and row['content_type'].startswith('image/'):
            from base64 import b64encode
            from avatar.files.parsing.images import jpeg_bytes
            try:
                image = jpeg_bytes(path)
            except HTTPException as exc:
                if exc.status_code!=415:
                    raise
                result['parse_error'] = str(exc.detail)
            else:
                result.update(coverage='visual',has_content=True,
                              image_input={'mime_type':'image/jpeg','data':b64encode(image).decode('ascii')})
        self.proofs[identifier] = {**row,
            'read_content':result['has_content'] or bool(previous and previous.get('read_content'))}
        if explicit or previous and previous.get('explicit_reference'):
            self.proofs[identifier]['explicit_reference'] = True
        return result

    def html(self, identifier, sha256):
        proof = self.proofs.get(identifier)
        if not proof or not proof.get('read_content') or proof['sha256']!=sha256 or proof['content_type']!='text/html':
            raise ValueError('请先用 read_session_file 读取当前会话旧 HTML，再使用其 ID 和 sha256')
        self.validate()
        if proof['size'] > MAX_SOURCE_BYTES:
            raise ValueError('原 HTML 超过 100 MiB 修订上限')
        data = self._original(proof).read_bytes()
        if hashlib.sha256(data).hexdigest()!=sha256:
            raise HTTPException(409, '会话文件原件已改变')
        return proof['filename'], data.decode('utf-8'), json.loads(proof['animation_metadata']).get('html_mode') or 'html'

    def sandbox_inputs(self):
        def read(row):
            self.validate()
            with self._original(row).open('rb') as stream:
                return stream.read(8*1024*1024+1)
        return {identifier:{'path':'inputs/'+identifier+'/'+Path(row['filename']).name,
                            'read':lambda row=row:read(row)} for identifier,row in self.proofs.items()
                if self.grants is None and row['origin']=='generated' and not row.get('listed_only')}
