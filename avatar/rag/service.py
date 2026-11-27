"""授权原件读取、PostgreSQL 混合检索与来源复核。"""
import asyncio
from avatar.core.execution import run_blocking
import hashlib
import json
import re
from dataclasses import replace
from avatar.core import execution
from contextlib import aclosing
from fastapi import HTTPException
from langchain_core.documents import Document
from avatar.models.embedding import EmbeddingSettings,embedding_window
from avatar.rag.reading import MaterialToolResult, ReadRequest, read_has_content, read_original_tool
from avatar.rag.indexing.service import FileRag,revision
from avatar.rag.invalidation import signature
from avatar.files.library.catalog import fit_catalog, fit_text_rows
from avatar.rag.query import FileQuery, file_catalog
from avatar.files.library.folders import with_folder_captions


SearchMaterialRequest = FileQuery


class MaterialRag(FileRag):
    def __init__(self, chat, *, agent_answer=None):
        super().__init__(chat)
        self.agent_answer = agent_answer


    @staticmethod
    def source_footer(context):
        if context[0].get('_omit_source_citations'):
            return ''
        sources=context[0].get('_material_sources',[])
        if not sources:return ''
        # 来源由本轮真正交给回答模型的证据生成；不把目录或未读取附件当作引用。
        rows=[]
        for source in sources:
            name=re.sub(r'[\r\n\x00-\x1f]+',' ',source['filename'])
            name=re.sub(r'([\\`*_{}\[\]()<>#!|])',r'\\\1',name)
            rows.append(name+' · '+source['type'])
        return '\n\n> 引用来源：\n'+''.join('> '+row+'  \n' for row in rows)

    def validate(self,context):
        proof=context[0].get('_rag_access')
        if not proof:return
        self._validate_revisions(proof['scope'], proof['revisions'], originals=proof.get('originals', False))

    async def validate_async(self, context):
        if context[0].get('_rag_access'):
            await run_blocking(self.validate, context)

    def _validate_revisions(self, scope, revisions, *, originals=False):
        expected = {tuple(key): value for key, value in revisions}
        rows = self.documents(*scope, file_keys=expected.keys(), revisions_only=True, originals=originals)
        fresh = {(row['source'], row['id']): revision(row) for row in rows}
        if fresh != expected:
            raise HTTPException(409,'文件或访问权限已变化，请重发问题')

    async def search_tool(self, scope, arguments):
        plan = SearchMaterialRequest.model_validate(arguments)
        documents = await run_blocking(self.documents, *scope, originals=True)
        catalog_filter = plan.model_copy(update={'section':'', 'page_start':None, 'page_end':None})
        catalog = [item for item in file_catalog(documents, plan.query) if catalog_filter.matches(item)]
        keys = {(item['source'], item['file_id']) for item in catalog}
        documents = [row for row in documents if (row['source'], row['id']) in keys]
        matches, notices = await self.retrieve(documents, plan.query, plan.model_copy(update={'filename':''}))
        evidence = fit_text_rows([{'text':item.page_content, **item.metadata} for item in matches], 4096, 'text')
        catalog = fit_catalog(catalog, plan.query, 2048)
        keys = {(item['source'], item['file_id']) for item in [*evidence, *catalog]}
        notices = [message for key, message in notices if key in keys]
        revisions = [((row['source'], row['id']), revision(row)) for row in documents
                     if (row['source'], row['id']) in keys]
        if revisions:
            await run_blocking(self._validate_revisions, scope, revisions, originals=True)
        sources = {item['file_id']:{'file_id':item['file_id'], 'filename':item['filename'], 'type':'RAG'}
                   for item in evidence if item.get('text', '').strip()}
        return MaterialToolResult({'evidence':evidence, 'catalog':catalog, 'notices':notices},
                                  revisions, list(sources.values()))

    async def read_tool(self, scope, arguments, *, vision=False, audio=False):
        request = ReadRequest.model_validate(arguments)
        rows = await run_blocking(self.documents, *scope,
                                       file_keys={('profile', request.file_id), ('chat', request.file_id)},
                                       originals=True)
        if not rows:
            raise HTTPException(404, '资料文件不存在或未授权')
        row = rows[0]
        media_type = row.get('content_type', '')
        path = await run_blocking(self.original_path, row, row['source'])
        result, images, sounds = await read_original_tool(
            path, row['filename'],
            media_type, request, vision=vision, audio=audio)
        revisions = [((row['source'], row['id']), revision(row))]
        await run_blocking(self._validate_revisions, scope, revisions, originals=True)
        sources = []
        if read_has_content(result) or images or sounds:
            sources.append({'file_id':row['id'], 'filename':row['filename'], 'type':'源文件'})
        content = {'file_id':row['id'], 'filename':row['filename'],
                   'relative_path':row.get('relative_path') or row['filename'], **result}
        return MaterialToolResult(content, revisions, sources, images, sounds)

    async def answer(self,context,message,*,progress=None,on_delta=None):
        await self.validate_async(context)
        if progress:
            for notice in dict.fromkeys(context[0].get('_workflow_notices',[])):
                if not notice.startswith('会话记忆已压缩，'):
                    progress({'type':'notice','message':notice})
        if context[0].get('_avatar_agent'):
            answer = await self.agent_answer(self, context, message, progress=progress, **({'on_delta':on_delta} if on_delta else {}))
        else:
            answer = await self.chat.model_client.generate(*context, message)
        await self.validate_async(context)
        if getattr(answer,'contact_draft',None) is not None or answer.finish_reason=='tool_required' or getattr(answer,'material_delivery',None) is not None:
            return answer
        return replace(answer,text=answer.text+self.source_footer(context),material_sources=tuple(context[0].get('_material_sources',[])))

    async def stream(self, context, message, *, progress=None):
        config = context[0]
        direct = config.get('_direct_chat')
        if not direct:
            await self.validate_async(context)
        if direct or not config.get('_avatar_agent'):
            async with aclosing(self.chat.model_client.stream(*context, message)) as source:
                async for event in source:
                    if not direct:
                        await self.validate_async(context)
                    yield event
            if not direct:
                await self.validate_async(context)
                if footer := self.source_footer(context):
                    yield {'type':'delta', 'text':footer}
            return

        events = asyncio.Queue()

        async def generate():
            delivered = 0
            digest = hashlib.sha256()

            def emit(text):
                nonlocal delivered
                delivered += len(text)
                digest.update(text.encode('utf-8', 'surrogatepass'))
                events.put_nowait({'type':'delta', 'text':text})

            def report(event):
                nonlocal delivered, digest
                if event.get('reset_reply'):
                    delivered = 0
                    digest = hashlib.sha256()
                    events.put_nowait(event)
                elif progress:
                    progress(event)

            try:
                result = await self.answer(context, message, progress=report, on_delta=emit)
                await self.validate_async(context)
                if progress:
                    progress({'type':'status', 'stage':'result_ready',
                              'message':'需要开启会话工具' if result.finish_reason=='tool_required' else '结果已生成'})
                if getattr(result, 'contact_draft', None) is not None:
                    events.put_nowait({'type':'contact_draft', 'document':result.contact_draft})
                if result.material_delivery is not None:
                    events.put_nowait({'type':'material_delivery', 'items':result.material_delivery})
                # Validate the provisional prefix without retaining every streamed token.
                confirmed = hashlib.sha256()
                chunk_chars = 8192
                if delivered <= len(result.text):
                    for start in range(0, delivered, chunk_chars):
                        confirmed.update(result.text[start:min(start+chunk_chars, delivered)].encode('utf-8', 'surrogatepass'))
                if delivered > len(result.text) or confirmed.digest() != digest.digest():
                    events.put_nowait({'type':'status', 'stage':'result_ready',
                                       'message':'已核实最终答复', 'reset_reply':True})
                    delivered = 0
                for start in range(delivered, len(result.text), chunk_chars):
                    events.put_nowait({'type':'delta', 'text':result.text[start:start+chunk_chars]})
                events.put_nowait({'type':'usage', 'usage':result.usage})
            finally:
                events.put_nowait(None)

        generator = asyncio.create_task(generate())
        try:
            while (event := await events.get()) is not None:
                yield event
            await generator
        finally:
            if not generator.done():
                generator.cancel()
            await asyncio.gather(generator, return_exceptions=True)

    def documents(self, owner_id, aic, session, selected, include_private, *, file_keys=None, revisions_only=False, originals=False):

        scope = execution.authorization()
        profile_ids = None if scope is None else set(scope.get('materials', []))
        chat_ids = None if scope is None else set(scope.get('files', []))
        if file_keys is not None:
            profile_ids = {key for source, key in file_keys if source == 'profile'
                           and (profile_ids is None or key in profile_ids)}
        selected = [(item['id'], item['user_id']) for item in selected
                    if (chat_ids is None or item['id'] in chat_ids)
                    and (file_keys is None or ('chat', item['id']) in file_keys)]
        with self.database.connect() as db:
            if not db.execute('SELECT 1 FROM avatar_records WHERE user_id=? AND aic=?', (owner_id, aic)).fetchone():
                raise HTTPException(404, '目标分身不存在')
            rows = []
            if profile_ids is None or profile_ids:
                fields = 'id,user_id,size,sha256,relative_path,caption' if revisions_only else '*'
                query = f"SELECT {fields} FROM attachments WHERE user_id=? AND (visibility='public' OR ?=1)"
                params = [owner_id, int(include_private)]
                if profile_ids is not None:
                    query += ' AND id IN (SELECT value FROM json_each(?))'
                    params.append(json.dumps(sorted(profile_ids)))
                rows = with_folder_captions(db, db.execute(query, params), owner_id)
                for row in rows:
                    row['source'] = 'profile'
            if selected:
                fields = 'id,user_id,conversation_id,size,sha256,description' if revisions_only else '*'
                parent = session.get('parent_id') or session['id']
                selection = json.dumps([{'id': identifier, 'owner': user_id} for identifier, user_id in selected])
                chat_rows = db.execute(f"SELECT {fields} FROM chat_files WHERE conversation_id=? AND referenced=1 "
                    "AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP) "
                    "AND (id,user_id) IN (SELECT (value::jsonb->>'id'),(value::jsonb->>'owner') "
                    "FROM json_each(?))", (parent, selection))
                chats = {(row['id'], row['user_id']): dict(row, source='chat', caption=row['description']) for row in chat_rows}
                rows.extend(chats[pair] for pair in selected if pair in chats)
        if originals:
            return [row for row in rows if self.original_available(row['source'],row['user_id'],row['id'],row['size'],row.get('conversation_id'))]
        return rows

    def _retrieval_inputs(self, owner, documents):
        """Read each authorized document and its current nodes in one database snapshot."""
        sources = [row['source'] for row in documents]
        ids = [row['id'] for row in documents]
        with self.database.connect() as db:
            config=EmbeddingSettings.current(db,owner)
            if not config.get('encrypted_key'):return config,None,{}
            model=self.chat.service.models.current(db,owner)
            rows = db.execute("""SELECT d.source,d.file_id,d.status,d.revision,d.signature,d.error,d.collection,
                ARRAY(SELECT n.record_id FROM file_rag_nodes n
                      WHERE n.user_id=d.user_id AND n.source=d.source AND n.file_id=d.file_id
                        AND d.status='ready' AND n.collection=d.collection ORDER BY n.ordinal) AS record_ids
                FROM unnest(?::text[],?::text[]) AS allowed(source,file_id)
                JOIN file_rag_documents d ON d.source=allowed.source AND d.file_id=allowed.file_id
                WHERE d.user_id=?""",(sources,ids,owner))
            return config,model,{(row['source'],row['file_id']):dict(row) for row in rows}

    def _live_search_keys(self, documents):
        if not documents:
            return set()
        columns = ([r['user_id'] for r in documents], [r['source'] for r in documents],
                   [r['id'] for r in documents], [r['sha256'] for r in documents],
                   [r.get('visibility')=='public' for r in documents], [r.get('conversation_id') for r in documents])
        with self.database.connect() as db:
            rows = db.execute("""WITH wanted(owner,source,id,sha,public_only,conversation_id) AS
                (SELECT * FROM unnest(?::text[],?::text[],?::text[],?::text[],?::boolean[],?::text[]))
                SELECT w.owner,w.source,w.id,a.size,w.conversation_id FROM wanted w JOIN attachments a ON a.user_id=w.owner AND a.id=w.id
                WHERE w.source='profile' AND a.sha256=w.sha AND (NOT w.public_only OR a.visibility='public')
                UNION ALL
                SELECT w.owner,w.source,w.id,f.size,f.conversation_id FROM wanted w JOIN chat_files f ON f.user_id=w.owner AND f.id=w.id
                WHERE w.source='chat' AND f.sha256=w.sha AND f.referenced=1 AND f.conversation_id=w.conversation_id
                AND (f.expires_at IS NULL OR avatar_utc_time(f.expires_at)>CURRENT_TIMESTAMP)""",columns)
            return {(r['source'],r['id']) for r in rows if self.original_available(r['source'],r['owner'],r['id'],r['size'],r['conversation_id'])}

    async def retrieve(self,documents,query,plan=None):
        plan=plan or FileQuery(query=query);candidates=[];notices=[]
        documents=[row for row in documents if plan.folder_matches(row)]
        snapshots={(row['source'],row['id']):row for row in documents}
        queries=list(dict.fromkeys([query,plan.query,*plan.alternatives]))[:3]
        for uid in sorted({row['user_id'] for row in documents}):
            owned = [row for row in documents if row['user_id']==uid]
            config,model,indexes=await run_blocking(self._retrieval_inputs,uid,owned)
            if not config.get('encrypted_key'):continue
            groups = {}
            token = signature(config,model)
            for row in owned:
                index = indexes.get((row['source'],row['id']))
                if not index or index['status']!='ready' or index['revision']!=revision(row) or index['signature'] != token:
                    notices.append(((row['source'],row['id']),('资料索引失败：'+index['error']+'；文件：' if index and index['status']=='failed' else '资料索引正在后台准备，可按需读取原件：')+row['filename']))
                    continue
                if index['record_ids']:
                    groups.setdefault(index['collection'],[]).extend(index['record_ids'])
            if not groups:continue
            vectors=[];width=max(64,embedding_window(config)-128)
            try:
                for text in queries:
                    parts=[text[start:start+width] for start in range(0,len(text),width)]
                    values=await self.vectors(uid,config,parts,purpose='rag_query')
                    vectors.append([sum(items)/len(values) for items in zip(*values,strict=True)])
            except HTTPException as error:
                if error.status_code not in (400,401,403,408,422,429,500,502,503,504):
                    raise
                # 嵌入不可用时继续使用已发布索引的 BM25 分支。
                vectors=[]
            for collection,identifiers in groups.items():
                rows=await run_blocking(self.store.search,uid,collection,identifiers,queries,vectors,plan.model_copy(update={'folder':''}))
                for row in rows:
                    metadata=row['metadata']
                    identity=(metadata['source'],metadata['file_id'])
                    snapshot=snapshots.get(identity)
                    if snapshot and snapshot['user_id']==uid:
                        candidates.append((identity,Document(page_content=row['text'],metadata={**metadata,
                            'filename':snapshot['filename'], 'relative_path':snapshot.get('relative_path') or snapshot['filename']})))
        # Vector and lexical caches know the published chunks; authorization belongs to the current original.
        shown={key for key,_ in candidates}|{key for key,_ in notices}
        live=await run_blocking(self._live_search_keys,[snapshots[key] for key in shown])
        matches=sorted((item for key,item in candidates if key in live),key=lambda item:item.metadata['score'],reverse=True)
        return matches[:12], list(dict.fromkeys((key, text) for key, text in notices if key in live))
