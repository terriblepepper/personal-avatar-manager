"""仅学习本人 Web 对话的用户气泡；独立风格记忆不随会话删除，用户已授权自动通过。"""
import json
import time
import uuid
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from avatar.core.db import dump,event,now
from avatar.models.model_config import require_complete
from avatar.models.embedding import EmbeddingSettings,require_embedding
from avatar.knowledge.style_traits import extract_style,style_evidence,StyleTraits,contextual_document


def auto_style_advance(db, owner, previous, checksum, current):
    """Allow only approved automatic descendants of the original, intact style."""
    return bool(db.execute("""WITH RECURSIVE lineage AS (
        SELECT id,document,parent_build,origin FROM soul_builds
        WHERE id=? AND user_id=? AND status='approved'
        UNION
        SELECT b.id,b.document,b.parent_build,b.origin FROM lineage head
        JOIN soul_builds b ON b.id=head.parent_build AND b.user_id=? AND b.status='approved'
        WHERE head.id IS DISTINCT FROM ?::text AND (
            (head.origin='model_refresh' AND head.document=b.document)
            OR (head.origin='learning' AND EXISTS (
                SELECT 1 FROM style_learning_runs r WHERE r.build_id=head.id AND r.user_id=b.user_id AND r.mode='auto')))
    )
    SELECT 1 FROM lineage b WHERE (b.id=? AND md5(b.document)=?)
       OR (?::text IS NULL AND b.parent_build='' AND b.origin='learning' AND EXISTS (
           SELECT 1 FROM style_learning_runs r WHERE r.build_id=b.id AND r.user_id=? AND r.mode='auto'))
    LIMIT 1""",(current,owner,owner,previous,previous,checksum,previous,owner)).fetchone())


NEEDS_ACTIVE_SOUL = '请先创建 Soul 并通过审核，待生效后再使用表达风格学习。'


class StyleLearning:
    def __init__(self,knowledge):self.knowledge=knowledge;self.database=knowledge.database;self.checked=0

    @staticmethod
    def conversation(db,user_id,identifier):
        row=db.execute("SELECT * FROM chat_conversations WHERE id=? AND user_id=? AND caller_id=? AND platform='web' AND parent_id IS NULL AND channel IN ('avatar','assistant')",(identifier,user_id,user_id)).fetchone()
        if not row:raise HTTPException(404,'本人对话不存在')
        return row

    @staticmethod
    def active_build(db, owner):
        row=db.execute("SELECT b.id FROM active_souls a JOIN soul_builds b ON b.id=a.build_id AND b.user_id=a.user_id "
                       "WHERE a.user_id=? AND b.status='approved'",(owner,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def reset_settings(db, owner):
        db.execute('UPDATE style_learning_settings SET enabled=0,updated_at=? WHERE user_id=? AND enabled=1',(now(),owner))

    def status(self,user,identifier):
        with self.database.connect() as db:
            self.conversation(db,user['id'],identifier)
            can_enable=self.active_build(db,user['id']) is not None
            setting=db.execute('SELECT * FROM style_learning_settings WHERE conversation_id=? AND user_id=?',(identifier,user['id'])).fetchone()
            last=db.execute("SELECT b.id,b.status,b.stage,b.error,b.updated_at FROM style_learning_runs r JOIN soul_builds b ON b.id=r.build_id,json_each(r.sources) s WHERE r.user_id=? AND json_extract(s.value,'$.conversation_id')=? ORDER BY r.created_at DESC LIMIT 1",(user['id'],identifier)).fetchone()
            memory=db.execute('SELECT updated_at FROM style_learning_memory WHERE user_id=?',(user['id'],)).fetchone()
        return {'enabled':can_enable and bool(setting and setting['enabled']),'can_enable':can_enable,'last':dict(last) if last else None,'learned_at':memory[0] if memory else None}

    def configure(self,user,identifier,enabled):
        with self.database.connect(write=True) as db:
            self.conversation(db,user['id'],identifier)
            if enabled and not self.active_build(db,user['id']):
                raise HTTPException(409,NEEDS_ACTIVE_SOUL)
            db.execute('INSERT INTO style_learning_settings VALUES(?,?,?,0,?) ON CONFLICT(conversation_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at',
                (identifier,user['id'],int(enabled),now()))
            if not enabled:
                ids=[r[0] for r in db.execute("SELECT r.build_id FROM style_learning_runs r,json_each(r.sources) s WHERE r.user_id=? AND r.mode='auto' AND json_extract(s.value,'$.conversation_id')=?",(user['id'],identifier))]
                for build_id in ids:
                    if db.execute("UPDATE soul_builds SET status='canceled',stage='已停用自动学习',updated_at=? WHERE id=? AND status IN ('queued','running')",(now(),build_id)).rowcount:
                        db.execute("UPDATE style_learning_runs SET samples='[]' WHERE build_id=?",(build_id,))
            event(db,user['id'],user['username'],'soul 持续学习设置',identifier,actor_id=user['id'])
        return self.status(user,identifier)

    def queue(self,user,identifier=None,automatic=False,day=None,conversation_ids=None):
        day=day or datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self.database.connect(write=True) as db:
            if identifier:self.conversation(db,user['id'],identifier)
            for cid in conversation_ids or ():self.conversation(db,user['id'],cid)
            parent=self.active_build(db,user['id'])
            if not parent:
                if automatic:return {'queued':False,'reason':'no_active_soul'}
                raise HTTPException(409,NEEDS_ACTIVE_SOUL)
            if automatic and db.execute("SELECT 1 FROM style_learning_runs WHERE user_id=? AND day=? AND mode='auto'",(user['id'],day)).fetchone():return {'queued':False,'reason':'today'}
            if db.execute("SELECT 1 FROM soul_builds WHERE user_id=? AND origin IN ('learning','model_refresh') AND status IN ('queued','running')",(user['id'],)).fetchone():
                if automatic:return {'queued':False,'reason':'running'}
                raise HTTPException(409,'请等待当前风格学习或索引重构完成')
            if identifier:
                db.execute('INSERT OR IGNORE INTO style_learning_settings VALUES(?,?,0,0,?)',(identifier,user['id'],now()))
            clauses=['c.user_id=?','s.user_id=c.user_id',"c.caller_id=c.user_id", "c.platform='web'",'c.parent_id IS NULL',"c.channel IN ('avatar','assistant')",
                "m.user_id=c.user_id","m.role='user'","m.channel IN ('avatar','assistant')",'m.id>s.last_message_id',"json_extract(m.task_event,'$.kind') IS NULL"]
            args=[user['id']]
            if automatic:clauses.append('s.enabled=1')
            elif conversation_ids:clauses.append('c.id IN (SELECT value FROM json_each(?))');args.append(dump(conversation_ids))
            else:clauses.append('c.id=?');args.append(identifier)
            rows=db.execute("SELECT m.id,m.content,c.id AS conversation_id FROM style_learning_settings s JOIN chat_conversations c ON c.id=s.conversation_id JOIN chat_messages m ON (m.conversation_id=c.id OR m.conversation_id IN (SELECT child.id FROM chat_conversations child WHERE child.parent_id=c.id AND child.user_id=c.user_id AND child.channel IN ('avatar','assistant'))) WHERE "+' AND '.join(clauses)+' ORDER BY m.id LIMIT 200',args).fetchall()
            samples=[];sources={};size=0
            for row in rows:
                if not row['content'].strip():continue
                if size+len(row['content'])>40000:break
                samples.append({'conversation_id':row['conversation_id'],'message_id':row['id'],'text':row['content']})
                sources[row['conversation_id']]=row['id'];size+=len(row['content'])
            if not samples:return {'queued':False,'reason':'no_new_messages'}
            account=db.execute('SELECT account_status FROM users WHERE id=?',(user['id'],)).fetchone()
            profile=db.execute('SELECT profile,last_modified_at FROM avatar_records WHERE user_id=?',(user['id'],)).fetchone()
            if not account or account[0]!='active' or not profile:raise HTTPException(409,'请先完成个人资料审核并创建分身')
            require_complete(self.knowledge.chat.service.models.current(db,user['id']))
            require_embedding(EmbeddingSettings.current(db,user['id']))
            build_id=str(uuid.uuid4());stamp=now()
            db.execute("""INSERT INTO soul_builds(id,user_id,prompt,asset_ids,profile_snapshot,profile_revision,status,stage,phase,origin,parent_build,created_at,updated_at)
              VALUES(?,?,?,'[]',?,?,'queued','等待学习新的本人气泡','learning','learning',?,?,?)""",
              (build_id,user['id'],'从新的本人气泡持续学习表达风格',profile['profile'],profile['last_modified_at'],parent,stamp,stamp))
            db.execute('INSERT INTO style_learning_runs VALUES(?,?,?,?,?,?,?)',(build_id,user['id'],'auto' if automatic else 'manual',day,
                dump([{'conversation_id':key,'through':value} for key,value in sources.items()]),dump(samples),stamp))
            event(db,user['id'],user['username'],'soul 持续学习开始',build_id,actor_id=user['id'])
        return {'queued':True,'id':build_id,'message_count':len(samples)}

    def retry(self,user):
        with self.database.connect() as db:
            row=db.execute("SELECT r.sources FROM style_learning_runs r JOIN soul_builds b ON b.id=r.build_id WHERE r.user_id=? AND b.status='failed' ORDER BY r.created_at DESC LIMIT 1",(user['id'],)).fetchone()
            if not row:raise HTTPException(409,'没有失败的学习任务')
            available={r[0] for r in db.execute("SELECT id FROM chat_conversations WHERE user_id=? AND caller_id=? AND platform='web' AND parent_id IS NULL",(user['id'],user['id']))}
            ids=[item['conversation_id'] for item in json.loads(row[0]) if item['conversation_id'] in available]
        if not ids:raise HTTPException(409,'原会话已清理，请在本人会话中学习新气泡')
        return self.queue(user,conversation_ids=ids)

    def schedule(self,day=None):
        if day is None and time.monotonic()-self.checked<60:return
        self.checked=time.monotonic()
        with self.database.connect() as db:
            users=[dict(r) for r in db.execute('''SELECT DISTINCT u.id,u.username
                FROM style_learning_settings s JOIN users u ON u.id=s.user_id
                JOIN active_souls a ON a.user_id=u.id JOIN soul_builds b ON b.id=a.build_id AND b.user_id=u.id
                WHERE s.enabled=1 AND u.account_status='active' AND b.status='approved' ''')]
        for user in users:
            try:self.queue(user,automatic=True,day=day)
            except HTTPException:continue

    async def process(self,row,config,key,worker):
        from avatar.knowledge.soul_document import FIELDS,approved_style
        k=worker
        with self.database.connect() as db:
            parent=self.active_build(db,row['user_id'])
            if not parent:raise HTTPException(409,NEEDS_ACTIVE_SOUL)
            if parent!=row['parent_build']:raise HTTPException(409,'生效 Soul 已变化，请重新发起风格学习')
            run=db.execute('SELECT * FROM style_learning_runs WHERE build_id=?',(row['id'],)).fetchone()
            if not run:raise HTTPException(409,'学习任务不存在')
            samples=json.loads(run['samples']);memory=db.execute('SELECT document FROM style_learning_memory WHERE user_id=?',(row['user_id'],)).fetchone()
            previous=json.loads(memory[0]) if memory else None;active=approved_style(db,row['user_id'])
        observations=[];chunks=[];chunk=[];size=0
        for sample in samples:
            text=sample['text']
            if chunk and size+len(text)>8000:chunks.append(chunk);chunk=[];size=0
            chunk.append(text);size+=len(text)
        if chunk:chunks.append(chunk)
        for index,chunk in enumerate(chunks):
            k.percent,k.stage=10+int(index/max(1,len(chunks))*65),'学习本人表达 '+str(index+1)+'/'+str(len(chunks))
            for observation in await extract_style(k,row,config,key,chunk):
                observations.append(observation)
        k.percent,k.stage=80,'整理 EverOS 本人风格记忆'
        memory=await k.memory.compile(row,config,key,{'new_style_observations':[style_evidence(item) for item in observations]})
        if not isinstance(memory.get('style_profile_traits'),list):raise HTTPException(502,'EverOS 未返回分层风格画像，未修改当前 soul')
        traits=[StyleTraits.model_validate(item).model_dump() for item in memory['style_profile_traits']]
        k.percent,k.stage=90,'更新独立风格记忆'
        # 只接纳封闭标签，最终表达也由标签确定；原始聊天不进入第二次自由生成。
        document=contextual_document(traits,active or previous)
        if not isinstance(document,dict) or set(document)!=set(FIELDS) or any(not isinstance(v,str) for v in document.values()) or sum(map(len,document.values()))>12000 or not document['voice'].strip():raise HTTPException(502,'学习结果格式无效，未修改已生效风格')
        with self.database.connect(write=True) as db:
            state=db.execute('SELECT status FROM soul_builds WHERE id=?',(row['id'],)).fetchone()
            if not state or state[0]!='running':return
            latest=self.active_build(db,row['user_id'])
            account=db.execute('SELECT account_status FROM users WHERE id=?',(row['user_id'],)).fetchone()
            if latest!=parent or not account or account[0]!='active':raise HTTPException(409,'生效版本或账户状态已变化，未覆盖新版本')
            inherited=db.execute('SELECT asset_ids,asset_captions,notes FROM soul_builds WHERE id=? AND user_id=?',(latest,row['user_id'])).fetchone()
            if inherited:db.execute('UPDATE soul_builds SET asset_ids=?,asset_captions=? WHERE id=?',(inherited['asset_ids'],inherited['asset_captions'],row['id']))
            stamp=now();notes=[{'filename':'本人对话用户气泡','coverage':f'本次 {len(samples)} 条；未使用助手、其他用户、任务或访客气泡'}]
            if inherited:notes=[*json.loads(inherited['notes']),*notes]
            db.execute("UPDATE soul_builds SET status='approved',phase='draft',progress=100,stage='学习完成，已自动更新风格',document=?,notes=?,reason='本人已授权对话风格学习自动通过',reviewed_at=?,updated_at=? WHERE id=?",(dump(document),dump(notes),stamp,stamp,row['id']))
            db.execute('INSERT INTO active_souls VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET build_id=excluded.build_id',(row['user_id'],row['id']))
            db.execute('UPDATE soul_builds SET observations=? WHERE id=?',(dump(traits),row['id']))
            db.execute('INSERT INTO style_learning_memory VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET document=excluded.document,updated_at=excluded.updated_at',(row['user_id'],dump(document),stamp))
            for source in json.loads(run['sources']):
                db.execute('UPDATE style_learning_settings SET last_message_id=MAX(last_message_id,?),updated_at=? WHERE conversation_id=? AND user_id=?',(source['through'],stamp,source['conversation_id'],row['user_id']))
            db.execute("UPDATE style_learning_runs SET samples='[]' WHERE build_id=?",(row['id'],))
            event(db,row['user_id'],'system','soul 持续学习自动通过',row['id'])
