from avatar.core.user_storage import UserBlobs
from avatar.files.uploads.buffer import upload_work, write_original
"""独立知识库：私有原料 → 可恢复后台构建 → 管理员审核 → 生效 soul。"""
import asyncio

import logging
import hashlib
import json
import uuid
from pathlib import Path
from fastapi import HTTPException
from avatar.accounts.auth import require_admin
from avatar.core.db import dump, event, now
from avatar.knowledge.media import MAX_ASSET, extract, require_asset_format, supports_asset, validate
from avatar.models.model_config import require_complete
from avatar.models.embedding import EmbeddingSettings,require_embedding
from avatar.knowledge.question_bank import QuestionBank
from avatar.knowledge.learning import StyleLearning
from avatar.knowledge import model_refresh
from avatar.knowledge.everos_memory import EverOSMemory
from avatar.knowledge.soul_document import FIELDS

CHOICES={'A':'完全符合','B':'比较符合','C':'比较不符合','D':'完全不符合','E':'其他'}



class KnowledgeService:
    def __init__(self, database, chat):
        self.database,self.chat=database,chat
        self.directory=UserBlobs(database,'knowledge','knowledge_assets')
        self.upload_lock=asyncio.Semaphore(2)
        from avatar.files.uploads.buffer import recover_removed
        recover_removed(database,self.directory,'knowledge_assets')
        self.bank=QuestionBank(database)
        self.learning=StyleLearning(self)
        self.memory=EverOSMemory(self)


    def asset(self,user_id,identifier,db=None):
        if db is None:
            with self.database.connect() as connection:return self.asset(user_id,identifier,connection)
        row=db.execute('SELECT * FROM knowledge_assets WHERE id=? AND user_id=?',(identifier,user_id)).fetchone()
        if not row:raise HTTPException(404,'知识库素材不存在')
        return dict(row)

    def file(self,user_id,identifier):
        row=self.asset(user_id,identifier);path=self.directory/(row['id']+'.blob')
        if not path.is_file():raise HTTPException(404,'素材原件已不存在')
        return path,row

    def view(self,user_id):
        with self.database.connect() as db:
            assets=[dict(r) for r in db.execute('SELECT * FROM knowledge_assets WHERE user_id=? ORDER BY created_at DESC',(user_id,))]
            builds=[self.decode(r) for r in db.execute("SELECT * FROM soul_builds WHERE user_id=? AND origin!='learning' ORDER BY created_at DESC LIMIT 100",(user_id,))]
            active=db.execute('SELECT build_id FROM active_souls WHERE user_id=?',(user_id,)).fetchone()
            if active and not any(item['id']==active[0] for item in builds):
                builds.append(self.decode(db.execute('SELECT * FROM soul_builds WHERE id=? AND user_id=?',(active[0],user_id)).fetchone()))
            learned=db.execute('SELECT updated_at FROM style_learning_memory WHERE user_id=?',(user_id,)).fetchone()
            learning=db.execute("SELECT id,status,stage,error,updated_at FROM soul_builds WHERE user_id=? AND origin='learning' ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
            categories=[{'id':r['id'],'name':r['name'],'count':r['available']} for r in self.bank.categories(db) if r['active']]
            previous=db.execute("SELECT prompt FROM soul_builds WHERE user_id=? AND origin NOT IN ('learning','model_refresh') ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
        for item in builds:
            if item['question_plan'] and not item['bank_answers']:
                item['bank_answers']={turn['id']:{'choice':turn.get('choice','E'),'other':turn.get('other',turn['answer']) if turn.get('choice','E')=='E' else ''}
                    for turn in item['interview'] if turn['kind']=='interview' and turn.get('id') and turn.get('answer') is not None}
            for key in ('profile_snapshot','observations','interview_observations','asset_captions'):item.pop(key,None)
        from avatar.knowledge.library import folders
        return {'quota':{'used_bytes':sum(r['size'] for r in assets if not r['source_attachment_id']),'limit_bytes':MAX_ASSET},
                'folders':folders(self,user_id),'learning':{**(dict(learning) if learning else {}),'learned_at':learned[0] if learned else None},'soul_id':user_id,'assets':assets,'builds':builds,'active_build_id':active[0] if active else None,'question_categories':categories,'last_prompt':previous[0] if previous else '',
                'model':self.chat.service.models.get(user_id)['model_config']}

    @staticmethod
    def decode(row):
        value=dict(row)
        for key in ('asset_ids','profile_snapshot','notes','document','interview','observations','asset_captions','question_plan','interview_observations','bank_answers','memory_tree','chat_samples'):value[key]=json.loads(value[key])
        return value

    def upload_budget(self,user_id):
        with self.database.connect() as db:
            used, largest = db.execute("""SELECT coalesce(sum(size) FILTER (WHERE source_attachment_id=''),0),
                coalesce(max(size),0) FROM knowledge_assets WHERE user_id=?""",(user_id,)).fetchone()
        # Even with a full account, an exact duplicate returns the existing asset.
        return min(MAX_ASSET,max(MAX_ASSET-used,largest))

    async def upload(self,user,filename,data,caption='',relative_path='', *, source_attachment_id='',batch_id=''):
        from avatar.files.library.attachments import MAX_STORED_SIZE
        async with self.upload_lock:
            ext,mime,kind=await upload_work(validate,filename,data,max_size=MAX_STORED_SIZE if source_attachment_id else MAX_ASSET)
            return await upload_work(self.store,user,filename,data,mime,kind,caption,relative_path,source_attachment_id,batch_id)

    def store(self,user,filename,data,mime,kind,caption,relative_path='',source_attachment_id='',batch_id=''):
        from avatar.files.library.folders import validate_path
        if relative_path:
            validate_path(relative_path)
            if relative_path.split('/')[-1]!=filename:raise HTTPException(422,'素材路径必须保留文件名')
        if len(caption)>2000:raise HTTPException(422,'素材说明最多 2000 字')
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.directory.chmod(0o700)
        identifier=str(uuid.uuid4());path=self.directory.new(user['id'],identifier+'.blob')
        checksum=write_original(path,data,extension=Path(filename).suffix.lower())
        try:
            with self.database.connect(write=True) as db:
                previous=db.execute('SELECT * FROM knowledge_assets WHERE user_id=? AND sha256=?',(user['id'],checksum)).fetchone()
                if source_attachment_id and not db.execute('SELECT 1 FROM attachments WHERE id=? AND user_id=? AND sha256=?',(source_attachment_id,user['id'],checksum)).fetchone():
                    raise HTTPException(409,'来源资料文件已改变，请重新引用')
                if previous:
                    if source_attachment_id:
                        db.execute('UPDATE knowledge_assets SET source_attachment_id=? WHERE id=?',(source_attachment_id,previous['id']))
                    if batch_id:
                        from avatar.files.uploads.batches import audit_detail
                        event(db,user['id'],user['username'],'上传知识库素材',audit_detail(previous['id'],batch_id),actor_id=user['id'])
                    from avatar.files.word_pdfs import discard_original
                    discard_original(path)
                    return self.asset(user['id'],previous['id'],db)
                total=db.execute("SELECT COALESCE(SUM(size),0) FROM knowledge_assets WHERE user_id=? AND source_attachment_id=''",(user['id'],)).fetchone()[0]
                if not source_attachment_id and total+len(data)>MAX_ASSET:
                    raise HTTPException(413,'Soul 上传素材总大小不能超过 10 GiB；从资料库引用不占用此额度')
                db.execute('INSERT INTO knowledge_assets(id,user_id,filename,mime,size,sha256,kind,caption,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                           (identifier,user['id'],filename,mime,len(data),checksum,kind,caption,now()))
                from avatar.files.library.folders import ensure_folders
                ensure_folders(db,user['id'],relative_path.rpartition('/')[0],table='knowledge_folders')
                db.execute('UPDATE knowledge_assets SET relative_path=?,source_attachment_id=? WHERE id=?',(relative_path or filename,source_attachment_id,identifier))
                from avatar.knowledge.library import changed
                changed(db,user['id'])
                from avatar.files.uploads.batches import audit_detail
                event(db,user['id'],user['username'],'上传知识库素材',audit_detail(identifier,batch_id),actor_id=user['id'])
                return self.asset(user['id'],identifier,db)
        except BaseException:
            from avatar.files.word_pdfs import discard_original
            discard_original(path)
            raise

    async def import_material(self,user,identifier,folder=''):
        """只复制本人资料原件，目标知识库始终私有，源可见性与生命周期不变。"""
        path,item=await asyncio.to_thread(self.chat.service.attachments.file_for,user,identifier)
        require_asset_format(item['filename'])
        from avatar.files.uploads.buffer import upload_buffer
        try:
            with path.open('rb') as source, upload_buffer(source) as data:
                return await self.upload(user,item['filename'],data,item.get('caption',''),(folder+'/' if folder else '')+item['filename'],source_attachment_id=identifier)
        except OSError:
            raise HTTPException(404,'资料文件已删除，请刷新后重新选择') from None

    def edit_caption(self,user,identifier,caption):
        with self.database.connect(write=True) as db:
            previous=self.asset(user['id'],identifier,db)
            db.execute('UPDATE knowledge_assets SET caption=? WHERE id=? AND user_id=?',(caption,identifier,user['id']))
            from avatar.knowledge.library import changed
            changed(db,user['id'],analyse=previous['caption']!=caption)
            event(db,user['id'],user['username'],'修改知识库素材说明',actor_id=user['id'])
            return self.asset(user['id'],identifier,db)

    def delete_asset(self,user,identifier):
        from avatar.files.library.batch import BatchRequest
        return self.batch(user,BatchRequest(action='delete',ids=[identifier]))

    def batch(self,user,body):
        if body.action!='delete':raise HTTPException(422,'Soul 素材始终私有，不支持公开权限')
        from avatar.files.library.batch import select
        from avatar.knowledge.library import check,changed,result
        from avatar.files.uploads.buffer import clean_removed
        originals=[]
        try:
            with self.database.connect(write=True) as db:
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
                    path=self.directory/(identifier+'.blob')
                    if path.exists():
                        staged=path.with_name('.removed-'+identifier+'-'+uuid.uuid4().hex)
                        path.replace(staged);originals.append((path,staged))
        except BaseException:
            for path,staged in originals:staged.replace(path)
            raise
        clean_removed(originals)
        return {**result(self,user['id']),'deleted':True,'changed_count':len(identifiers)}

    def create(self,user,prompt,asset_ids,interview_rounds=3,conversation_ids=(),rebuild=False):
        if interview_rounds not in (0,3,5):raise HTTPException(422,'每类题量只能选择 3 或 5 道')
        with self.database.connect(write=True) as db:
            if asset_ids:
                identifiers = list(dict.fromkeys(asset_ids))
            else:
                rows = db.execute('SELECT id,filename FROM knowledge_assets WHERE user_id=? ORDER BY created_at,id',(user['id'],))
                identifiers = [row['id'] for row in rows if supports_asset(row['filename'])]
            if not prompt.strip() and not identifiers and not interview_rounds and not conversation_ids:
                raise HTTPException(422,'请填写塑造提示、选择知识库素材或本人聊天')
            account=db.execute('SELECT account_status FROM users WHERE id=?',(user['id'],)).fetchone()
            profile=db.execute('SELECT profile,last_modified_at FROM avatar_records WHERE user_id=?',(user['id'],)).fetchone()
            if not account or account[0]!='active' or not profile:raise HTTPException(409,'请先完成个人资料审核并创建分身')
            if db.execute("SELECT 1 FROM soul_builds WHERE user_id=? AND status IN ('queued','running','waiting_user','ready','pending')",(user['id'],)).fetchone():
                raise HTTPException(409,'已有进行中的构建，请继续问答、提交审核或先取消')
            if db.execute("SELECT COUNT(*) FROM soul_builds WHERE user_id=? AND origin NOT IN ('learning','model_refresh')",(user['id'],)).fetchone()[0]>=100:
                raise HTTPException(409,'构建记录已达 100 项，请先删除不需要的历史记录')
            from avatar.files.library.folders import with_folder_caption
            captions = {}
            for identifier in identifiers:
                asset = self.asset(user['id'],identifier,db)
                require_asset_format(asset['filename'])
                captions[identifier] = with_folder_caption(db,asset,table='knowledge_folders')['caption']
            require_complete(self.chat.service.models.current(db,user['id']))
            require_embedding(EmbeddingSettings.current(db,user['id']))
            plan=self.bank.draw(db,interview_rounds) if interview_rounds else []
            samples=[]
            for cid in dict.fromkeys(conversation_ids):
                self.learning.conversation(db,user['id'],cid)
                for message in db.execute("SELECT id,content FROM chat_messages WHERE user_id=? AND role='user' AND channel IN ('avatar','assistant') AND (conversation_id=? OR conversation_id IN (SELECT id FROM chat_conversations WHERE parent_id=? AND user_id=? AND caller_id=? AND platform='web')) AND json_extract(task_event,'$.kind') IS NULL ORDER BY id LIMIT 201",(user['id'],cid,cid,user['id'],user['id'])):
                    if message['content'].strip():samples.append({'conversation_id':cid,'message_id':message['id'],'text':message['content']})
            if len(samples)>200 or sum(len(item['text']) for item in samples)>40000:raise HTTPException(422,'所选聊天最多 200 条本人气泡、40000 字，请减少所选会话')
            base=db.execute('SELECT b.id,b.document FROM active_souls a JOIN soul_builds b ON b.id=a.build_id WHERE a.user_id=?',(user['id'],)).fetchone()
            identifier=str(uuid.uuid4());stamp=now()
            db.execute('''INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,created_at,updated_at,interview_rounds,asset_captions,question_plan,followup_rounds,parent_build,document)
                          VALUES(?,?,?,?,?,?,'queued','等待后台构建',?,?,?,?,?,?,?,?)''',
                       (identifier,user['id'],prompt.strip(),dump(identifiers),profile['profile'],profile['last_modified_at'],stamp,stamp,interview_rounds,dump(captions),dump(plan),interview_rounds,base['id'] if base else '',base['document'] if base else '{}'))
            event(db,user['id'],user['username'],'开始构建 soul',identifier,actor_id=user['id'])
            db.execute('UPDATE soul_builds SET chat_samples=?,rebuild=? WHERE id=?',(dump(samples),int(rebuild),identifier))
            if rebuild:db.execute("UPDATE soul_builds SET document='{}' WHERE id=?",(identifier,))
        return {'id':identifier,'status':'queued'}

    def rebuild_index(self,user):
        with self.database.connect(write=True) as db:
            identifier=model_refresh.schedule(db,user['id'],manual=True)
            event(db,user['id'],user['username'],'重构已获批的 Soul 索引',identifier,actor_id=user['id'])
            status=db.execute('SELECT status FROM soul_builds WHERE id=?',(identifier,)).fetchone()[0]
        return {'id':identifier,'status':status}

    def retry(self,user,identifier):
        """Resume the failed snapshot without redrawing questions or discarding answers."""
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT * FROM soul_builds WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
            if not row:raise HTTPException(404,'构建记录不存在')
            if row['status']!='failed' or row['origin']=='learning':raise HTTPException(409,'只能重试失败的 soul 构建')
            if row['origin']==model_refresh.ORIGIN:
                flags=json.loads(row['memory_tree'])['refresh']
                queued=model_refresh.schedule(db,user['id'],embedding=flags['embedding'],visual=flags['visual'],manual=flags['manual'])
                if not queued:raise HTTPException(409,'当前 Soul 没有符合条件的素材或嵌入模型')
                return {'id':queued,'status':'queued'}
            if db.execute("SELECT 1 FROM soul_builds WHERE user_id=? AND status IN ('queued','running','waiting_user','ready','pending')",(user['id'],)).fetchone():
                raise HTTPException(409,'已有进行中的构建，请先处理该构建')
            profile=db.execute('SELECT a.profile,u.account_status FROM avatar_records a JOIN users u ON u.id=a.user_id WHERE a.user_id=?',(user['id'],)).fetchone()
            if not profile or profile['account_status']!='active':raise HTTPException(409,'请先完成个人资料审核并创建分身')
            if json.loads(profile['profile'])!=json.loads(row['profile_snapshot']):raise HTTPException(409,'个人资料已改变，请重新创建 soul；原问答记录仍保留')
            for asset in json.loads(row['asset_ids']):require_asset_format(self.asset(user['id'],asset,db)['filename'])
            require_complete(self.chat.service.models.current(db,user['id']))
            require_embedding(EmbeddingSettings.current(db,user['id']))
            db.execute("UPDATE soul_builds SET status='queued',error='',stage='等待重试，保留已完成问答',updated_at=? WHERE id=? AND status='failed'",(now(),identifier))
            event(db,user['id'],user['username'],'开始构建 soul',identifier,actor_id=user['id'])
        return {'id':identifier,'status':'queued'}

    def cancel(self,user,identifier):
        with self.database.connect(write=True) as db:
            if not db.execute("UPDATE soul_builds SET status='canceled',stage='已取消',updated_at=? WHERE id=? AND user_id=? AND status IN ('queued','running','waiting_user','ready','pending')",(now(),identifier,user['id'])).rowcount:
                raise HTTPException(409,'只能取消进行中或待审核的版本')
            db.execute("UPDATE style_learning_runs SET samples='[]' WHERE build_id=?",(identifier,))
            event(db,user['id'],user['username'],'取消 soul 构建',actor_id=user['id'])
        return {'status':'canceled'}

    def questionnaire(self,user,identifier,answers,revision,submit=False):
        with self.database.connect(write=True) as db:
            record=db.execute('SELECT * FROM soul_builds WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
            if not record:raise HTTPException(404,'构建记录不存在')
            row=self.decode(record)
            if row['status']!='waiting_user' or row['phase'] not in ('questionnaire','interview') or not row['question_plan'] or row['updated_at']!=revision:
                raise HTTPException(409,'题库作答状态已更新，请重新打开小窗')
            known={item['id'] for item in row['question_plan']};result={}
            for item in answers:
                if item.id not in known or item.id in result:raise HTTPException(422,'答案包含重复或不属于本次构建的题目')
                if item.choice!='E' and item.other:raise HTTPException(422,'只有 E 选项可以填写自定义答案')
                if submit and item.choice=='E' and not item.other.strip():raise HTTPException(422,'选择 E 时请填写答案')
                result[item.id]={'choice':item.choice,'other':item.other}
            if sum(len(value['other']) for value in result.values())>120000:raise HTTPException(422,'本次自填答案合计最多 120000 字')
            if submit and set(result)!=known:raise HTTPException(422,'请完成全部题目后再提交')
            stamp=now()
            if submit:
                interview=[{**item,'kind':'interview','choice':result[item['id']]['choice'],'other':result[item['id']]['other'],
                    'answer':result[item['id']]['other'] if result[item['id']]['choice']=='E' else CHOICES[result[item['id']]['choice']]}
                    for item in row['question_plan']]
                db.execute("UPDATE soul_builds SET bank_answers=?,interview=?,phase='followup',status='queued',stage='题库已完成，准备模型补充提问',interview_observations='[]',analyzed_answers=0,updated_at=? WHERE id=?",
                    (dump(result),dump(interview),stamp,identifier))
                event(db,user['id'],user['username'],'提交 soul 题库答案',identifier,actor_id=user['id'])
            else:
                db.execute('UPDATE soul_builds SET bank_answers=?,updated_at=? WHERE id=?',(dump(result),stamp,identifier))
        return {'id':identifier,'status':'queued' if submit else 'waiting_user','revision':stamp}

    def answer(self,user,identifier,answer,revision):
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT * FROM soul_builds WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
            if not row:raise HTTPException(404,'构建记录不存在')
            if row['status'] not in ('waiting_user','ready','pending') or row['updated_at']!=revision:
                raise HTTPException(409,'问题或构建状态已更新，请刷新后再提交')
            row=self.decode(row);interview=row['interview']
            if len(interview)>=max(16,len(row['question_plan'])+row['followup_rounds']+12) or sum(len(item.get('answer') or '') for item in interview)+len(answer)>120000:
                raise HTTPException(422,'本次问答已达上限，请整理提示词后重新构建')
            if row['status']=='waiting_user':
                if row['question_plan'] and row['phase']!='followup':raise HTTPException(409,'请在题库作答小窗统一提交答案')
                if not interview or interview[-1].get('answer') is not None:raise HTTPException(409,'当前没有待回答的问题')
                interview[-1]['answer']=answer
                phase='followup' if row['phase']=='followup' else 'interview'
            else:
                interview.append({'kind':'clarification','question':'','answer':answer})
                phase='analysis' if row['phase']=='analysis' and row['asset_ids'] else 'draft'
            db.execute("UPDATE soul_builds SET interview=?,phase=?,status='queued',stage='已收到补充，等待继续分析',error='',reviewer=NULL,reason='',reviewed_at=NULL,updated_at=? WHERE id=?",
                       (dump(interview),phase,now(),identifier))
            event(db,user['id'],user['username'],'补充 soul 风格问答',identifier,actor_id=user['id'])
        return {'id':identifier,'status':'queued'}

    def submit(self,user,identifier):
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT * FROM soul_builds WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
            if not row:raise HTTPException(404,'构建记录不存在')
            if row['status']!='ready':raise HTTPException(409,'请先完成问答并生成待确认草案')
            profile=db.execute('SELECT profile FROM avatar_records WHERE user_id=?',(user['id'],)).fetchone()
            if not profile or json.loads(profile[0])!=json.loads(row['profile_snapshot']):raise HTTPException(409,'个人资料已经改变，请取消后重新构建')
            db.execute("UPDATE soul_builds SET status='pending',stage='等待管理员审核',updated_at=? WHERE id=?",(now(),identifier))
            event(db,user['id'],user['username'],'soul 构建完成，等待审核',identifier,actor_id=user['id'])
        return {'id':identifier,'status':'pending'}

    def refine(self,user,identifier,document,revision,asset_ids=(),prompt=None,memory_edits=(),tree_snapshot=None):
        document={**document,'uncertainties':''}
        if set(document)!=set(FIELDS) or not document['voice'].strip() or any(len(value)>4000 for value in document.values()) or sum(map(len,document.values()))>12000:
            raise HTTPException(422,'请保留完整 soul 字段，语气不能为空；单项最多 4000 字、总计最多 12000 字')
        with self.database.connect(write=True) as db:
            record=db.execute('SELECT * FROM soul_builds WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
            if not record:raise HTTPException(404,'构建记录不存在')
            if record['status'] not in ('ready','pending','approved','rejected') or record['updated_at']!=revision:
                raise HTTPException(409,'soul 状态已更新，请刷新后再修改')
            source=self.decode(record);requested=list(dict.fromkeys(asset_ids));old=source['asset_ids']
            available = {item['id'] for item in db.execute('SELECT id,filename FROM knowledge_assets WHERE user_id=?',(user['id'],))
                         if supports_asset(item['filename'])}
            retained = [asset for asset in old if asset in available]
            from avatar.files.library.folders import with_folder_caption
            captions = {asset:caption for asset,caption in source['asset_captions'].items() if asset in available}
            for identifier in requested:
                asset = self.asset(user['id'],identifier,db)
                require_asset_format(asset['filename'])
                captions[identifier] = with_folder_caption(db,asset,table='knowledge_folders')['caption']
            changed_captions=any(captions.get(asset)!=source['asset_captions'].get(asset) for asset in requested if asset in old)
            additions=[asset for asset in requested if asset not in old]
            assets=[*retained,*additions]
            instructions=record['prompt'] if prompt is None else prompt.strip()
            active=db.execute('SELECT build_id FROM active_souls WHERE user_id=?',(user['id'],)).fetchone()
            if record['status']=='approved' and (not active or active[0]!=record['id']):raise HTTPException(409,'请从当前 soul 的“继续完善”入口进行微调')
            if memory_edits and (not tree_snapshot or tree_snapshot.get('active_build_id','')!=(active[0] if active else '')):raise HTTPException(409,'生效记忆已更新，请重新打开后修订')
            regenerate=bool(additions or changed_captions or instructions!=record['prompt'] or memory_edits)
            if regenerate:
                require_complete(self.chat.service.models.current(db,user['id']))
                require_embedding(EmbeddingSettings.current(db,user['id']))
            stamp=now();status='queued' if regenerate else 'ready'
            stage='等待分析新增素材与微调要求' if regenerate else '微调草案已保存，可确认后提交审核'
            updates={'asset_ids':dump(assets),'asset_captions':dump(captions),'prompt':instructions,'status':status,'phase':'analysis' if regenerate else 'draft',
                'origin':'refine','progress':0 if regenerate else 100,'stage':stage,'document':dump(document),
                'reviewer':None,'reviewed_at':None,'reason':'','error':'','updated_at':stamp,'rebuild':0}
            if changed_captions or (regenerate and any(asset not in available for asset in old)):
                updates.update(notes='[]',observations='[]')
            if record['status'] in ('approved','rejected'):
                if db.execute("SELECT 1 FROM soul_builds WHERE user_id=? AND status IN ('queued','running','waiting_user','ready','pending')",(user['id'],)).fetchone():raise HTTPException(409,'已有进行中的修订，请先处理该修订')
                if db.execute("SELECT COUNT(*) FROM soul_builds WHERE user_id=? AND origin NOT IN ('learning','model_refresh')",(user['id'],)).fetchone()[0]>=100:raise HTTPException(409,'修订记录已达上限，请先清理历史记录')
                profile=db.execute('SELECT profile,last_modified_at FROM avatar_records WHERE user_id=?',(user['id'],)).fetchone()
                if not profile:raise HTTPException(409,'请先完成个人资料审核')
                original=identifier;identifier=str(uuid.uuid4())
                values={**dict(record),**updates,'id':identifier,'parent_build':original,'profile_snapshot':profile['profile'],'profile_revision':profile['last_modified_at'],'created_at':stamp}
                db.execute('INSERT INTO soul_builds ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
            else:
                db.execute('UPDATE soul_builds SET '+','.join(name+'=?' for name in updates)+' WHERE id=?',(*updates.values(),identifier))
            memory={**source['memory_tree'],'project':self.memory.project(source)}
            if identifier!=record['id'] and record['status']=='approved':memory={'project':self.memory.project(source)}
            if regenerate:
                changes={item['id']:item for item in memory.get('edits',[])};changes.update({item['id']:item for item in memory_edits})
                memory={**memory,'project':'build-'+hashlib.sha256(identifier.encode()).hexdigest()[:32],'source_project':self.memory.project(source),'base_build':active[0] if active else '', 'edits':list(changes.values())}
            db.execute('UPDATE soul_builds SET memory_tree=? WHERE id=?',(dump(memory),identifier))
            event(db,user['id'],user['username'],'开始构建 soul' if regenerate else '微调 soul 草案',identifier,actor_id=user['id'])
        return {'id':identifier,'status':status,'soul_id':user['id']}

    def delete_build(self,user,identifier):
        with self.database.connect(write=True) as db:
            row=db.execute('SELECT status FROM soul_builds WHERE id=? AND user_id=?',(identifier,user['id'])).fetchone()
            if not row:raise HTTPException(404,'构建记录不存在')
            if row[0] in ('queued','running','waiting_user','ready','pending') or db.execute('SELECT 1 FROM active_souls WHERE build_id=?',(identifier,)).fetchone():
                raise HTTPException(409,'请先取消构建或停用当前 soul 后再删除')
            db.execute('DELETE FROM soul_builds WHERE id=?',(identifier,))
        return {'deleted':True}

    def delete_all_builds(self,user):
        with self.database.connect(write=True) as db:
            count=db.execute("DELETE FROM soul_builds WHERE user_id=? AND origin!='learning' AND status NOT IN ('queued','running','waiting_user','ready','pending') AND NOT EXISTS(SELECT 1 FROM active_souls a WHERE a.build_id=soul_builds.id)",(user['id'],)).rowcount
        return {'deleted':count}

    def disable(self,user):
        with self.database.connect(write=True) as db:
            db.execute('DELETE FROM active_souls WHERE user_id=?',(user['id'],))
            self.learning.reset_settings(db,user['id'])
            event(db,user['id'],user['username'],'停用分身 soul',actor_id=user['id'])
        return {'active_build_id':None}

    def review(self,admin,identifier,decision,db):
        require_admin(admin)
        current=db.execute('SELECT role FROM users WHERE id=?',(admin['id'],)).fetchone()
        if not current:raise HTTPException(403,'需要管理员权限')
        require_admin(current)
        row=db.execute('SELECT * FROM soul_builds WHERE id=?',(identifier,)).fetchone()
        if not row or row['status']!='pending':raise HTTPException(409,'soul 审核状态已变更')
        if not decision.approve and not decision.reason.strip():raise HTTPException(422,'退回时请填写原因')
        profile=db.execute('SELECT profile FROM avatar_records WHERE user_id=?',(row['user_id'],)).fetchone()
        if decision.approve and (not profile or json.loads(profile[0])!=json.loads(row['profile_snapshot'])):
            raise HTTPException(409,'构建后个人资料已经改变，请退回后重新构建')
        tree=json.loads(row['memory_tree']);active=self.learning.active_build(db,row['user_id'])
        if decision.approve and tree.get('edits') and tree.get('base_build','')!=(active or ''):raise HTTPException(409,'生效记忆已改变，请退回后基于新记忆重新修订')
        status='approved' if decision.approve else 'rejected';stamp=now()
        db.execute('UPDATE soul_builds SET status=?,stage=?,reviewer=?,reason=?,reviewed_at=?,updated_at=? WHERE id=?',
                   (status,'审核通过' if decision.approve else '审核退回',admin['id'],decision.reason,stamp,stamp,identifier))
        if decision.approve:
            if not active:self.learning.reset_settings(db,row['user_id'])
            db.execute('INSERT INTO active_souls VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET build_id=excluded.build_id',(row['user_id'],identifier))
            db.execute('UPDATE style_learning_memory SET document=?,updated_at=? WHERE user_id=?',(row['document'],stamp,row['user_id']))
        event(db,row['user_id'],admin['username'],'soul 审核通过' if decision.approve else 'soul 审核退回',decision.reason,actor_id=admin['id'])
