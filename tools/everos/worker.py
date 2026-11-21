"""EverOS 1.5.1 私有进程桥；Markdown 为源，PostgreSQL 存 entry 索引与 real[] 向量。"""
import asyncio
import hashlib
import json
import os
import re
import shutil
from datetime import datetime,timezone
from pathlib import Path
import sys
import time
import uuid

wire=sys.stdout
sys.stdout=sys.stderr
os.umask(0o077)


def checked(root,path):
    if path.resolve()!=path or not path.is_relative_to(root.root):raise ValueError('Invalid private memory path')
    return path


def manifest_data(value):
    if isinstance(value,list):value={'processed':value}
    if not isinstance(value,dict) or not isinstance(value.get('processed',[]),list) or not all(isinstance(key,str) for key in value.get('processed',[])) or not isinstance(value.get('profile_signature',''),str):raise ValueError('Invalid source manifest')
    return value


def emit_done(result):
    print(json.dumps({'type':'done','result':result},ensure_ascii=False),file=wire,flush=True)


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def _content_sha256(named_parts):
    return hashlib.sha256('\n'.join(f'{key}={named_parts[key]}' for key in sorted(named_parts)).encode()).hexdigest()


def _inline_list(value):
    if isinstance(value,list):return [str(item) for item in value]
    text=str(value or '').strip()
    if text.startswith('[') and text.endswith(']'):text=text[1:-1]
    return [item.strip().strip('"\'') for item in text.split(',') if item.strip()]


class ProjectFiles:
    """同用户共享索引锁内，先发布待提交目录，再恢复发布中的文件。"""
    def __init__(self,root,owner,project):
        if not re.fullmatch(r'[a-f0-9]{64}',owner) or not re.fullmatch(r'(?:learning|build-[a-f0-9]{32})',project):raise ValueError('Invalid memory scope')
        self.root=root
        self.directory=checked(root,root.users_dir('avatar-soul',project)/owner)
        self.state=checked(root,root.root/'.avatar-worker'/owner/project)

    def recover(self):
        pending=checked(self.root,self.state/'pending')
        if not pending.exists():return
        self.directory.parent.mkdir(parents=True,exist_ok=True)
        if not self.directory.exists() and not (pending/'.remove-profile').exists():
            os.replace(pending,self.directory);return
        remove_profile=checked(self.root,pending/'.remove-profile')
        if remove_profile.is_file():
            checked(self.root,self.directory/'user.md').unlink(missing_ok=True);remove_profile.unlink()
        for source in sorted(pending.rglob('*')):
            checked(self.root,source)
            if source.is_file():
                target=checked(self.root,self.directory/source.relative_to(pending));target.parent.mkdir(parents=True,exist_ok=True);os.replace(source,target)
        shutil.rmtree(pending)

    def publish(self,changes):
        self.recover();preparing=checked(self.root,self.state/'preparing')
        if preparing.exists():shutil.rmtree(preparing)
        preparing.mkdir(parents=True)
        for name,content in changes.items():
            target=checked(self.root,preparing/name);target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('wb') as handle:
                if isinstance(content,Path):
                    with checked(self.root,content).open('rb') as source:shutil.copyfileobj(source,handle)
                else:handle.write(content.encode('utf-8'))
                handle.flush();os.fsync(handle.fileno())
        os.replace(preparing,self.state/'pending');self.recover()

    @staticmethod
    def fingerprint(payload):
        inputs={key:value for key,value in payload.items() if not key.startswith('_') and key!='root'}
        if inputs.get('engine')=='everos-native-v2':inputs.pop('source_id',None)
        return hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()

    def result(self,payload,stage='result'):
        path=checked(self.root,self.state/(stage+'.json'))
        if path.is_file():
            record=json.loads(path.read_text())
            if record['fingerprint']==self.fingerprint(payload):return record['result']
        return None

    def save_result(self,payload,result,stage='result'):
        if len(json.dumps(result,ensure_ascii=False))>(40000 if stage=='result' else 2*1024*1024):raise ValueError('Memory result exceeds budget')
        self.state.mkdir(parents=True,exist_ok=True);temporary=checked(self.root,self.state/'result.tmp')
        with temporary.open('w',encoding='utf-8') as handle:
            json.dump({'fingerprint':self.fingerprint(payload),'result':result},handle,ensure_ascii=False);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,self.state/(stage+'.json'))
        if stage=='result':
            for previous in ('episode','profile'):(self.state/(previous+'.json')).unlink(missing_ok=True)


async def memory_tree(root,owner,project,edits=()):
    from everos.core.persistence.markdown.entries import render_structured_entry
    from everos.core.persistence.markdown.frontmatter import dump_frontmatter
    from everos.core.persistence.markdown.reader import MarkdownReader
    files=ProjectFiles(root,owner,project);directory=checked(root,files.directory/'episodes');nodes=[];updates={}
    changes={item['id']:item for item in edits};matched=set()
    for path in sorted(directory.glob('episode-*.md')):
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):raise ValueError('Invalid memory path')
        if path.stat().st_size>2*1024*1024:raise ValueError('Memory file exceeds budget')
        parsed=await MarkdownReader.read(path);body=parsed.body;deprecated=parsed.frontmatter.get('deprecated_entries',{})
        for entry in reversed(parsed.entries):
            identifier=hashlib.sha256((path.name+'#'+entry.id).encode()).hexdigest();structured=entry.as_structured();sections=structured.sections;change=changes.get(identifier)
            if change:
                matched.add(identifier)
                if change['deleted']:deprecated[entry.id]='user-reviewed-removal'
                else:
                    updated=render_structured_entry(header=structured.header,inline=structured.inline,sections={**sections,'Subject':change['title'],'Summary':change['summary'],'Content':change['content']})
                    body=body[:entry.start]+f'<!-- entry:{entry.id} -->\n{updated}\n<!-- /entry:{entry.id} -->'+body[entry.end:]
                    sections={**sections,'Subject':change['title'],'Summary':change['summary'],'Content':change['content']}
            if entry.id not in deprecated:nodes.append({'id':identifier,'entry_id':entry.id,'group':path.stem.removeprefix('episode-'),'title':sections.get('Subject',entry.id),'summary':sections.get('Summary',''),'content':sections.get('Content','')})
        if any(hashlib.sha256((path.name+'#'+entry.id).encode()).hexdigest() in changes for entry in parsed.entries):updates['episodes/'+path.name]=dump_frontmatter({**parsed.frontmatter,'deprecated_entries':deprecated})+body
    if matched!=set(changes):raise ValueError('Unknown memory node')
    if len(nodes)>1000 or len(json.dumps(nodes,ensure_ascii=False))>2*1024*1024:raise ValueError('Memory tree exceeds budget')
    if updates:files.publish(updates)
    return sorted(nodes,key=lambda item:(item['group'],item['id']),reverse=True)


async def seed_tree(root,owner,source,target):
    from everos.core.persistence.markdown.reader import MarkdownReader
    if not source or source==target:return
    origin=ProjectFiles(root,owner,source);origin.recover();target_files=ProjectFiles(root,owner,target);target_files.recover()
    marker=checked(root,target_files.directory/'.avatar-seed.json')
    if marker.is_file():
        if json.loads(marker.read_text())!=source:raise ValueError('Seed source changed')
        return
    directory=checked(root,origin.directory/'episodes')
    if not origin.directory.is_dir():raise ValueError('Seed source missing')
    paths=list(directory.glob('episode-*.md'))
    for optional in (directory/'.avatar-sources.json',origin.directory/'user.md'):
        checked(root,optional)
        if optional.exists():paths.append(optional)
    existing_episodes=checked(root,target_files.directory/'episodes');names={path.relative_to(origin.directory) for path in paths}
    existing_paths=list(existing_episodes.glob('episode-*.md'))+[existing_episodes/'.avatar-sources.json',target_files.directory/'user.md'];missing={}
    changed=any(checked(root,path).exists() and path.relative_to(target_files.directory) not in names for path in existing_paths)
    for path in paths:
        checked(root,path)
        if not path.is_file() or path.stat().st_size>2*1024*1024:raise ValueError('Invalid memory source')
        content=path.read_text()
        if path.suffix=='.json':manifest_data(json.loads(content))
        else:
            parsed=await MarkdownReader.read(path)
            if parsed.frontmatter.get('user_id',owner)!=owner:raise ValueError('Invalid seed owner')
        name=path.relative_to(origin.directory);existing=checked(root,target_files.directory/name)
        if existing.exists():changed|=existing.read_text()!=content
        else:missing[name]=path
    if changed and missing:raise ValueError('Ambiguous legacy memory seed')
    missing['.avatar-seed.json']=json.dumps(source);target_files.publish(missing)


def evidence_items(evidence):
    result={}
    for name,value in evidence.items():
        if name=='source_assets':continue
        for item in value if isinstance(value,list) else [value]:
            if not item or isinstance(item,dict) and not any(item.values()):continue
            data={name:item};result[_digest(data)]=data
    return result


def storage_lock(path):
    from everos.core.persistence.memory_root import MemoryRoot
    from everos.core.persistence.locking import memory_root_lock
    return memory_root_lock(MemoryRoot(path))


def _episode_record(owner,project,md_path,entry,tokenizer):
    structured=entry.as_structured();sections=structured.sections;inline=structured.inline
    subject=str(sections.get('Subject') or '');summary=str(sections.get('Summary') or '');content=str(sections.get('Content') or '')
    digest=_content_sha256({'section:Subject':subject,'section:Summary':summary,'section:Content':content})
    timestamp=inline.get('timestamp') or datetime.now(timezone.utc).isoformat()
    tokens=' '.join(tokenizer.tokenize(f'{content} {subject}' if subject else content))
    return {'kind':'episode','record_id':f'{owner}_{entry.id}','entry_id':entry.id,'md_path':md_path,
            'content_sha256':digest,'group':Path(md_path).stem.removeprefix('episode-'),'title':subject or entry.id,
            'summary':summary,'content':content,'tokens':tokens,'embedding':None,'subject_embedding':None,
            'metadata':{'owner_id':owner,'owner_type':'user','app_id':'avatar-soul','project_id':project,
                        'session_id':inline.get('session_id'),'timestamp':timestamp,'parent_type':inline.get('parent_type','memcell'),
                        'parent_id':inline.get('parent_id',''),'sender_ids':_inline_list(inline.get('sender_ids',''))}}


def _profile_record(owner,project,profile):
    front=profile[0];summary=str(front.summary or '');explicit=getattr(front,'explicit_info',[]);traits=getattr(front,'implicit_traits',[])
    digest=_content_sha256({'frontmatter:summary':summary,'frontmatter:explicit_info_json':json.dumps(explicit,sort_keys=True,ensure_ascii=False),'frontmatter:implicit_traits_json':json.dumps(traits,sort_keys=True,ensure_ascii=False)})
    profile_data={'summary':summary,'explicit_info':explicit,'implicit_traits':traits,'profile_timestamp_ms':front.profile_timestamp_ms}
    return {'kind':'user_profile','record_id':owner,'entry_id':'','md_path':'user.md','content_sha256':digest,'group':'profile','title':'长期风格画像','summary':summary,'content':summary,'embedding':None,'subject_embedding':None,'metadata':{'owner_id':owner,'owner_type':'user','app_id':'avatar-soul','project_id':project,'profile_data':profile_data}}


async def _records_for_path(files,owner,project,md_path,profile_reader,tokenizer):
    from everos.core.persistence.markdown.reader import MarkdownReader
    from everos.infra.persistence.markdown import UserProfileFrontmatter
    if md_path=='user.md':
        profile=await profile_reader.read(owner,schema=UserProfileFrontmatter,app_id='avatar-soul',project_id=project)
        return [_profile_record(owner,project,profile)] if profile else []
    path=checked(files.root,files.directory/md_path);parsed=await MarkdownReader.read(path)
    return [_episode_record(owner,project,md_path,entry,tokenizer) for entry in parsed.entries]


EPISODE_VECTOR_FIELDS = (('content','embedding'),('title','subject_embedding'))


def _missing_episode_embeddings(record):
    return [(vector,record[source]) for source,vector in EPISODE_VECTOR_FIELDS
            if record.get(vector) is None and record.get(source,'').strip()]


def _reuse_embeddings(records,existing,embedding_enabled):
    prior={row['record_id']:row for row in existing}
    changed=[];to_embed=[]
    for record in records:
        old=prior.get(record['record_id'],{})
        same_record=old.get('content_sha256')==record['content_sha256']
        if record['kind']=='episode':
            for source,vector in EPISODE_VECTOR_FIELDS:
                text=record.get(source,'')
                if text.strip() and text==old.get(source):
                    record[vector]=old.get(vector)
            missing=embedding_enabled and bool(_missing_episode_embeddings(record))
            if missing:to_embed.append(record)
        else:
            if same_record:
                record['embedding']=old.get('embedding');record['subject_embedding']=old.get('subject_embedding')
            missing=embedding_enabled and (record.get('embedding') is None or
                                            record.get('title') and record.get('subject_embedding') is None)
        if not same_record or missing or any(record.get(vector)!=old.get(vector) for _,vector in EPISODE_VECTOR_FIELDS):
            changed.append(record)
    return changed,to_embed


async def _embed_records(embedder,records,embedding_enabled):
    if not embedding_enabled:return
    inputs=[(record,field,text) for record in records for field,text in _missing_episode_embeddings(record)]
    for start in range(0,len(inputs),10):
        batch=inputs[start:start+10];vectors=await embedder.embed_batch([text for _,_,text in batch])
        if len(vectors)!=len(batch):raise ValueError('EverOS vector response incomplete')
        for (record,field,_),vector in zip(batch,vectors,strict=True):record[field]=vector


async def main(payload):
    from everos.core.persistence.memory_root import MemoryRoot
    root=MemoryRoot(payload['root']);files=ProjectFiles(root,payload['owner'],payload['project'])
    async with storage_lock(files.state):
        if (files.state/'pending').exists():
            async with storage_lock(checked(root,root.root/'.avatar-worker')):files.recover()
        if payload.get('operation')=='tree':emit_done({'nodes':await memory_tree(root,payload['owner'],payload['project'])});return
        result=files.result(payload)
        if result is None:
            result=await compile_memory(payload,root,files);files.save_result(payload,result)
        emit_done(result)


async def compile_memory(payload,root,files):
    from everos.component.tokenizer import build_tokenizer
    from everos.infra.persistence.markdown import ProfileReader
    tokenizer=build_tokenizer()
    lock=asyncio.Lock();extraction_key=None;llm_kind='llm'
    async def rpc(kind,**values):
        async with lock:
            print(json.dumps({'type':kind,**values},ensure_ascii=False),file=wire,flush=True);line=await asyncio.to_thread(sys.stdin.readline)
            if not line:raise RuntimeError('Parent disconnected')
            result=json.loads(line)
            if 'error' in result:raise RuntimeError('EverOS storage failed')
            return result['result']
    class LLM:
        async def chat(self,messages,*,response_format=None,**kwargs):
            result=await rpc(llm_kind,messages=[m.model_dump(mode='json') for m in messages],schema=response_format.model_json_schema() if response_format else None,cache_key=extraction_key)
            parsed=response_format.model_validate_json(result) if response_format else None
            return __import__('everos.component.llm',fromlist=['ChatResponse']).ChatResponse(content=result,model='account-configured',parsed=parsed,finish_reason='stop')
    class Embedder:
        async def embed_batch(self,texts):
            result=[]
            for start in range(0,len(texts),10):result.extend(await rpc('embed',texts=list(texts[start:start+10])))
            return result
        async def embed(self,text):return (await self.embed_batch([text]))[0]
    llm=LLM();embedder=Embedder()
    async def pg(operation,**values):
        return await rpc('pg',operation=operation,owner=payload['owner'],index_signature=payload['index_signature'],project=payload['project'],**values)
    await pg('ensure');index_scope=checked(root,root.root/'.avatar-worker')
    async with storage_lock(index_scope):
        root.ensure();await seed_tree(root,payload['owner'],payload.get('source_project'),payload['project']);await memory_tree(root,payload['owner'],payload['project'],payload.get('edits',[]))
    if payload.get('source_project') and payload['source_project']!=payload['project']:await pg('copy_vectors',source=payload['source_project'])
    directory=root.users_dir('avatar-soul',payload['project'])/payload['owner']/'episodes';directory.mkdir(parents=True,exist_ok=True)
    manifest=directory/'.avatar-sources.json';saved=manifest_data(json.loads(manifest.read_text()) if manifest.is_file() else [])
    processed=set(saved.get('processed',[]));profile_signature=saved.get('profile_signature','');items=evidence_items(payload['evidence']);delta={key:value for key,value in items.items() if key not in processed}
    stamp=int(time.time()*1000);session=payload['source_id']
    from everos.core.persistence.markdown.writer import MarkdownWriter
    def journal_writer(state):
        class JournalWriter(MarkdownWriter):
            async def write(self,path,content):
                async with storage_lock(index_scope):files.publish({path.relative_to(files.directory):content,'episodes/.avatar-sources.json':json.dumps(state)})
                return path
        return JournalWriter(root)
    async def remember(sections,keys):
        from everos.infra.persistence.markdown import EpisodeWriter
        next_processed=sorted(processed.union(keys));writer=EpisodeWriter(root,writer=journal_writer({**saved,'processed':next_processed}))
        await writer.append_entry(payload['owner'],inline={'owner_id':payload['owner'],'session_id':session,'timestamp':datetime.fromtimestamp(stamp/1000,timezone.utc).isoformat(),'parent_type':'memcell','parent_id':session,'sender_ids':[payload['owner']]},sections=sections,app_id='avatar-soul',project_id=payload['project'])
        processed.update(keys);saved['processed']=next_processed
    if delta:
        from everalgo.types import MemCell
        from everalgo.user_memory import EpisodeExtractor
        payload['_stage']='episode';extraction_key=hashlib.sha256(json.dumps(sorted(delta)).encode()).hexdigest();cell=MemCell.model_validate({'timestamp':stamp,'items':[{'kind':'text','id':session,'sender_id':payload['owner'],'role':'user','timestamp':stamp,'content':json.dumps(list(delta.values()),ensure_ascii=False)}]});sections=files.result(payload,'episode')
        if sections is None:
            episode=await EpisodeExtractor(llm=llm).aextract(cell,sender_id=None);sections={'Subject':episode.subject,'Summary':getattr(episode,'summary',episode.episode[:200]),'Content':episode.episode};files.save_result(payload,sections,'episode')
        extraction_key=None;await remember(sections,delta)
    from everos.infra.persistence.markdown import ProfileReader,ProfileWriter,UserProfileFrontmatter
    profile_reader=ProfileReader(root);profile=await profile_reader.read(payload['owner'],schema=UserProfileFrontmatter,app_id='avatar-soul',project_id=payload['project'])
    if profile and payload.get('edits') and not await memory_tree(root,payload['owner'],payload['project']):
        saved.update(profile_signature='',profile_edits='');files.publish({'.remove-profile':'','episodes/.avatar-sources.json':json.dumps(saved)});profile=None;profile_signature=''
    observations=payload['evidence'].get('new_style_observations');update_signature=hashlib.sha256(json.dumps(observations,sort_keys=True).encode()).hexdigest() if observations else profile_signature;edits_key=hashlib.sha256(json.dumps(payload.get('edits',[]),sort_keys=True).encode()).hexdigest()
    if observations and update_signature!=profile_signature or profile and payload.get('edits') and edits_key!=saved.get('profile_edits'):
        from everalgo.types import MemCell,Profile
        from everalgo.user_memory import ProfileExtractor
        old=Profile.model_validate({'owner_id':payload['owner'],'summary':profile[0].summary,'timestamp':profile[0].profile_timestamp_ms,'explicit_info':profile[0].explicit_info,'implicit_traits':profile[0].implicit_traits}) if profile else None;evidence={'style_observations':observations or [],'reviewed_memory_edits':payload.get('edits',[])};cell=MemCell.model_validate({'timestamp':stamp,'items':[{'kind':'text','id':session,'sender_id':payload['owner'],'role':'user','timestamp':stamp,'content':json.dumps(evidence,ensure_ascii=False)}]});payload['_stage']='profile';llm_kind='profile'
        try:
            protocol='UPDATE：只输出 {{"operations":[{{"action":"add|update|delete|none","type":"implicit_traits","index":0,"data":{{"traits":{{}}}}}}]}}。add/update 的 data.traits 为封闭风格标签。' if old else 'INIT：只输出 {{"explicit_info":[],"implicit_traits":[{{"traits":{{}}}}]}}。traits 为封闭风格标签。';prompt=('仅维护目标用户 {target_user} 的表达风格。样本只包含已校验的安全风格标签。结合场景与心情分层学习；不同场景保留，不以暂时情绪覆盖长期习惯。禁止事实、身份、原话及任意自由字段。'+protocol+'\n已有画像：'+('{current_profile}' if old else '无')+'\n新增安全观察：'+('{conversations}' if old else '{conversation_text}')+'\n严格遵守调用方给出的 traits JSON Schema，不添加通用人格字段。');prepared=files.result(payload,'profile')
            if prepared is None:
                updated=await ProfileExtractor(llm=llm).aextract([cell],sender_id=payload['owner'],old_profile=old,prompt=prompt);prepared=UserProfileFrontmatter(id='profile_'+payload['owner'],user_id=payload['owner'],summary=updated.summary,profile_timestamp_ms=updated.timestamp,explicit_info=getattr(updated,'explicit_info',[]),implicit_traits=getattr(updated,'implicit_traits',[])).model_dump(mode='json');files.save_result(payload,prepared,'profile')
        finally:llm_kind='llm'
        frontmatter=UserProfileFrontmatter.model_validate(prepared);saved.update(profile_signature=update_signature,profile_edits=edits_key);await ProfileWriter(root,writer=journal_writer(saved)).write(payload['owner'],frontmatter=frontmatter,body=frontmatter.summary,app_id='avatar-soul',project_id=payload['project']);profile=await profile_reader.read(payload['owner'],schema=UserProfileFrontmatter,app_id='avatar-soul',project_id=payload['project'])
    for media in payload.get('media',[]):
        media_key='media:'+hashlib.sha256(json.dumps(media,sort_keys=True).encode()).hexdigest()
        if media_key not in processed:await remember({'Subject':media['filename'],'Summary':media['caption'] or '原图直接嵌入；未生成文字转写','Content':media['marker']},[media_key])
    payload['_stage']='index';queue_rows=[]
    for path in sorted(directory.glob('episode-*.md')):queue_rows.append({'md_path':'episodes/'+path.name,'kind':'episode','mtime':path.stat().st_mtime})
    profile_path=files.directory/'user.md'
    if profile_path.is_file():queue_rows.append({'md_path':'user.md','kind':'user_profile','mtime':profile_path.stat().st_mtime})
    async with storage_lock(index_scope):
        await pg('queue_sync',rows=queue_rows);failures=[];processed_any=True
        while processed_any:
            claimed=(await pg('queue_claim',limit=500))['rows'];processed_any=bool(claimed)
            for row in claimed:
                try:
                    if row['change_type']=='deleted':await pg('delete_file',md_path=row['md_path'])
                    else:
                        records=await _records_for_path(files,payload['owner'],payload['project'],row['md_path'],profile_reader,tokenizer);existing=(await pg('existing_vectors',md_path=row['md_path']))['rows'];changed,to_embed=_reuse_embeddings(records,existing,bool(payload.get('embedding_enabled')));await _embed_records(embedder,to_embed,bool(payload.get('embedding_enabled')))
                        if changed:
                            generation=uuid.uuid4().hex;await pg('vector_begin',generation=generation)
                            for start in range(0,len(records),8):await pg('vector_chunk',generation=generation,records=records[start:start+8])
                            await pg('vector_commit_file',generation=generation,md_path=row['md_path'])
                    await pg('queue_complete',rows=[{'md_path':row['md_path'],'status':'done'}])
                except Exception as error:
                    retryable=not isinstance(error,(ValueError,KeyError,TypeError));failures.append(type(error).__name__);await pg('queue_complete',rows=[{'md_path':row['md_path'],'status':'failed','retryable':retryable,'retry_count':int(row.get('retry_count',0))+1,'error':type(error).__name__}])
        if failures:raise RuntimeError('EverOS index incomplete: '+','.join(sorted(set(failures))))
    payload['_stage']='search'
    query='本人表达风格、语气、措辞、句式、习惯和最新修正'
    vector_ready=bool(payload.get('embedding_enabled')) and (await pg('has_episode_vectors'))['ready']
    method='hybrid' if vector_ready else 'keyword'
    query_vector=await embedder.embed(query) if vector_ready else None
    found=(await pg('search',query=query,query_tokens=tokenizer.tokenize(query),method=method,
                    embedding=query_vector,limit=6,include_profile=False))['rows']
    profile_row=(await pg('profile'))['row']
    episodes=[]
    for row in found:
        metadata=row.get('metadata') or {};episodes.append({'id':row['record_id'],'user_id':metadata.get('owner_id'),'app_id':'avatar-soul','project_id':payload['project'],'session_id':metadata.get('session_id'),'timestamp':metadata.get('timestamp'),'sender_ids':metadata.get('sender_ids',[]),'summary':row.get('summary',''),'subject':row.get('title',''),'episode':row.get('content',''),'type':'Conversation','score':row['score'],'atomic_facts':[]})
    profiles=[]
    if profile_row:
        metadata=profile_row.get('metadata') or {};profiles.append({'id':profile_row['record_id'],'user_id':metadata.get('owner_id'),'app_id':'avatar-soul','project_id':payload['project'],'profile_data':metadata.get('profile_data',{}),'score':None})
    traits=[item['traits'] for item in profile[0].implicit_traits if isinstance(item,dict) and isinstance(item.get('traits'),dict)] if profile else []
    from everos import __version__
    return {'backend':'EverOS '+__version__,'method':method,'memories':{'episodes':episodes,'profiles':profiles,'agent_cases':[],'agent_skills':[],'unprocessed_messages':[]},'style_profile_traits':traits}


if __name__=='__main__':
    payload=json.loads(sys.stdin.readline())
    try:asyncio.run(main(payload))
    except BaseException as exc:
        import traceback
        traceback.print_exc(file=sys.stderr);print(json.dumps({'type':'error','stage':payload.get('_stage','startup'),'code':type(exc).__name__}),file=wire,flush=True);sys.exit(1)
