"""Tool bindings for a temporary ACPs turn using the same agent loop as Web chat."""
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from fastapi import HTTPException

from avatar.conversation.loop.entry_tools import bind_entry_tools
from avatar.conversation.loop.runtime import restore_files
from avatar.conversation.tools.material_delivery import FindMaterial
from avatar.conversation.tools.material_search import MaterialDiscovery
from avatar.core.execution import run_blocking
from avatar.files.conversation.resources import validate_output
from avatar.files.conversation.session_files import SessionFiles, usable_text
from avatar.files.library.delivery import SavedMaterials
from avatar.files.library.public import PublicMaterials, SEND_PUBLIC_MATERIAL
from avatar.files.parsing import PLAIN_TEXT_EXTENSIONS


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
            if extension in ('.pdf','.doc','.docx','.ppt','.pptx','.xlsx','.avanim'):
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

    def has_html_source(self):
        return super().has_html_source() or any(fid in self.staged_reads and row['mimeType']=='text/html'
            and row['path'].is_file() for fid,row in self.previous.items())

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
    public = PublicMaterials(chat)
    materials = SavedMaterials(chat.service.attachments,owner)
    exports = list(state.get('town_exports',()))
    visible = set(state.get('town_visible',()))
    discovery = None

    def allowed():
        return visible | {item.get('file_id') for item in config.get('_material_sources',())}

    def public_input(identifier):
        with materials.database.connect() as db:
            row = db.execute("""SELECT filename,size,sha256 FROM attachments WHERE id=? AND user_id=?
                AND visibility='public' AND saved_at IS NOT NULL FOR SHARE""",(identifier,owner)).fetchone()
            if not row:
                raise HTTPException(409,'沙箱公开原件的授权已变化')
            source = materials.directory.new(owner,identifier+'.blob')
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
                    (list(wanted-{None}),owner,8*1024*1024)):
                    fid = row['id']
                    inputs[fid] = {'path':f"inputs/{fid}/{row['filename']}",'read':lambda fid=fid:public_input(fid)}
        return inputs

    async def sandbox(context, objective):
        inputs = await run_blocking(sandbox_catalog)
        return await chat.skill_execution.enrich(owner,context,objective,chat.model_client,caller_id=caller,
            conversation_id=conversation['id'],parent_id=conversation['id'],inputs=inputs,
            progress=progress,allow_code=True)

    config.update(_html_enabled=True,_sandbox_https_enabled=False,_sandbox_workspace=path/'python',
                  _town_exports=exports)
    model = await bind_entry_tools(chat,model,owner,actor['aic'],conversation,[],progress=progress,
                                   sandbox=sandbox,temporary_files=workspace)
    entry = config['_execute_entry_tool']

    async def execute(name,args):
        nonlocal discovery
        if name=='find_public_material':
            request = FindMaterial.model_validate(args)
            if discovery is None:
                def pull():
                    return materials.documents(public_only=True)
                discovery = MaterialDiscovery(await run_blocking(pull),chat.rag.retrieve,refresh=pull)
            result = await discovery.page(request)
            visible.update(item['file_id'] for item in result['items'])
            await run_blocking(store.merge,{'town_visible':sorted(visible)})
            return json.dumps(result,ensure_ascii=False)
        if name=='send_public_material':
            try:
                items = await run_blocking(public.prepare,owner,args)
            except HTTPException as exc:
                if exc.status_code!=404:
                    raise
                raise ValueError(str(exc.detail)) from None
            if any(item['public_material_id'] not in allowed() for item in items):
                raise ValueError('请先检索公开资料目录，再选择可见的原件 ID')
            known = {item['public_material_id'] for item in exports}
            items = [item for item in items if item['public_material_id'] not in known]
            if len(items)+len(exports)>5:
                raise ValueError('本轮最多交付五份公开资料原件')
            exports.extend(items)
            await run_blocking(store.merge,{'town_exports':exports})
            return json.dumps({'files':[{'id':item['id'],'filename':item['filename']} for item in exports]},ensure_ascii=False)
        return await entry(name,args)

    config['_entry_tools'] += [{'name':'find_public_material','description':'混合查找公开资料的真实文件名、说明和路径，可分页浏览目录。',
                                'parameters':FindMaterial.model_json_schema()}, SEND_PUBLIC_MATERIAL]
    config['_entry_lookup_types'].update(find_public_material=FindMaterial)
    config['_execute_entry_tool'] = execute
    return model

