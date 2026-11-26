"""Tool bindings for a temporary ACPs turn using the same agent loop as Web chat."""
import hashlib
import subprocess
import uuid
from pathlib import Path

from fastapi import HTTPException

from avatar.conversation.loop.entry_tools import bind_entry_tools, EntryToolContext
from avatar.conversation.loop.runtime import restore_files
from avatar.conversation.loop.public_materials import bind_public_material_tools
from avatar.core.execution import run_blocking
from avatar.files.conversation.resources import validate_output
from avatar.files.conversation.session_files import SessionFiles, usable_text
from avatar.files.library.delivery import SavedMaterials
from avatar.files.parsing import PLAIN_TEXT_EXTENSIONS
from avatar.skills.python_tool import AtomicPython


class TemporarySessionFiles(SessionFiles):
    def __init__(self, chat, owner, outputs, path, reads, previous):
        super().__init__(chat.chat_files, owner, None, {}, outputs=outputs, staged_reads=reads)
        self.path = path
        self.previous = previous

    def discover(self, query, offset):
        value = query.casefold()
        documents = [(item['id'],item['filename'],len(item['data'])) for item in reversed(self.outputs)]
        documents.extend((item['id'],item['name'],item['size']) for item in self.previous.values()
                         if item['path'].is_file())
        matches = [{'id':identifier,'filename':name,'size':size,'origin':'generated','can_read':True}
                   for identifier,name,size in documents if value in (name+' '+identifier).casefold()]
        return {'kind':'directory', 'has_content':False, 'items':matches[offset:offset+10],
                'next_offset':offset+10 if len(matches)>offset+10 else None}

    def staged_html(self, identifier):
        item = next((item for item in self.outputs if item['id']==identifier), None)
        if item and Path(item['filename']).suffix.lower() in ('.html','.htm'):
            return {'html_mode':'html', **item}
        return None

    def read(self, identifier, offset, page, *, sandbox, explicit=False, limit=None, vision=False):
        item = next((item for item in self.outputs if item['id']==identifier), None)
        path = None
        if item:
            data = item['data']
            name = item['filename']
            size = len(data)
            digest = self.staged_digest(item)
            mime = validate_output(name,data)
        elif (previous := self.previous.get(identifier)) and previous['path'].is_file():
            path = previous['path']
            name, size, digest, mime = (previous[key] for key in ('name','size','sha256','mimeType'))
        else:
            raise ValueError('当前临时工作区没有该文件，请重新查目录')
        extension = Path(name).suffix.lower()
        result = {'kind':'file', 'id':identifier, 'filename':name, 'size':size, 'sha256':digest,
                  'content_type':mime, 'source':'chat', 'origin':'generated', 'has_content':False}
        limit = limit or 8192
        if mime.startswith('text/') or extension in PLAIN_TEXT_EXTENSIONS:
            self._check_text_cursor(offset,page,size)
            if path:
                with path.open('rb') as stream:
                    stream.seek(offset)
                    chunk = stream.read(limit)
            else:
                chunk = data[offset:offset+limit]
            result.update(self._text_page(chunk,offset,size))
            if usable_text(result.get('text')):
                self.staged_reads[identifier] = digest
                result['has_content'] = True
        else:
            if path is None:
                path = self.path / str(uuid.UUID(identifier))
                if not path.exists():
                    path.write_bytes(data)
            if extension in ('.pdf','.doc','.docx','.ppt','.pptx','.xlsx'):
                from avatar.files.parsing.text_worker import read_original
                try:
                    parsed = read_original(path,extension,start=page,end=page,offset=offset,limit=max(1,limit//4))
                except (subprocess.SubprocessError, OSError, ValueError):
                    parsed = {'coverage':'metadata_only','parse_error':'文档正文解析失败或超时'}
                result.update(parsed)
                result['has_content'] = any(usable_text(part.get('text')) for part in parsed.get('pages',()))
                if not parsed.get('next') and page<parsed.get('total_pages',0):
                    result.update(next={'start':page+1,'offset':0}, complete=False)
            if vision and mime.startswith('image/'):
                from base64 import b64encode
                from avatar.files.parsing.images import jpeg_bytes
                result.update(coverage='visual',has_content=True,
                              image_input={'mime_type':'image/jpeg','data':b64encode(jpeg_bytes(path)).decode('ascii')})
        if not result['has_content']:
            result['sandbox'] = ('可以将文件 ID 交给 Python 读取原始字节。' if size<=8*1024*1024
                                 else '原件超出沙箱输入上限。')
        return result

    def html(self, identifier, sha256):
        if previous := self.previous.get(identifier):
            if (self.staged_reads.get(identifier)!=sha256 or previous['sha256']!=sha256
                    or previous['mimeType']!='text/html' or not previous['path'].is_file()):
                raise ValueError('请先用 read_session_file 读取本任务的 HTML 原件和摘要')
            data = previous['path'].read_bytes()
            if hashlib.sha256(data).hexdigest()!=sha256:
                raise ValueError('此前的 HTML 原件已改变')
            return previous['name'],data.decode('utf-8'),'html'
        return super().html(identifier,sha256)

    def sandbox_inputs(self):
        def read(item):
            with item['path'].open('rb') as stream:
                data = stream.read(8*1024*1024+1)
            if hashlib.sha256(data).hexdigest()!=item['sha256']:
                raise ValueError('此前的工作区原件已改变')
            return data
        inputs = {item['id']:{'path':f"inputs/{item['id']}/{item['name']}",'read':lambda item=item:read(item)}
                  for item in self.previous.values() if item['size']<=8*1024*1024 and item['path'].is_file()}
        inputs.update({item['id']:{'path':f"inputs/{item['id']}/{item['filename']}",
                                   'read':lambda item=item:item['data']} for item in self.outputs})
        return inputs


async def bind_town_tools(chat, model, actor, caller, conversation, path, store, progress, previous):
    config, key, system, history = model
    state = await run_blocking(store.load)
    await restore_files(store,state,config)
    outputs = config.setdefault('_generated_files',[])
    owner = actor['id']
    workspace = TemporarySessionFiles(chat,owner,outputs,path,state.get('staged_html_reads'),previous)
    materials = SavedMaterials(chat.service.attachments,owner)

    def public_input(identifier):
        with materials.database.connect() as db:
            row = db.execute("""SELECT filename,size,sha256 FROM attachments WHERE id=? AND user_id=?
                AND visibility='public' AND saved_at IS NOT NULL FOR SHARE""",(identifier,owner)).fetchone()
            if not row:
                raise HTTPException(409,'沙箱公开原件的授权已变化')
            source = materials.attachments.directory.new(owner,identifier+'.blob')
            with source.open('rb') as stream:
                data = stream.read(8*1024*1024+1)
            if len(data)!=row['size'] or hashlib.sha256(data).hexdigest()!=row['sha256']:
                raise HTTPException(409,'沙箱公开原件大小超限或内容已变化')
            return data

    def sandbox_catalog():
        wanted = allowed()
        inputs = workspace.sandbox_inputs()
        if wanted:
            with materials.database.connect() as db:
                for row in db.execute("""SELECT id,filename FROM attachments WHERE id=ANY(?::text[]) AND user_id=?
                    AND visibility='public' AND saved_at IS NOT NULL AND size<=?""",
                    (list(wanted),owner,8*1024*1024)):
                    fid = row['id']
                    inputs[fid] = {'path':f"inputs/{fid}/{row['filename']}",'read':lambda fid=fid:public_input(fid)}
        return inputs

    def sandbox(_):
        return AtomicPython(chat.sandbox, config, owner=owner, conversation=conversation['id'], caller=caller,
                            parent=conversation['id'], inputs=sandbox_catalog, progress=progress)

    config.update(_html_enabled=True,_sandbox_https_enabled=False,_sandbox_workspace=path/'python')
    allowed = bind_public_material_tools(chat, config, materials, owner, store, state, allow_sources=True)
    return await bind_entry_tools(chat, model, EntryToolContext(owner, actor['aic'], conversation, []),
                                  progress=progress,sandbox=sandbox,temporary_files=workspace)
