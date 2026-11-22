"""配对会话资源授权；使用现有聊天文件格式、解析器、下载及资料转存接口。"""
import json
import logging
import os
import re
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from fastapi import HTTPException
from avatar.core.db import now
from avatar.files.conversation.resources import original_hashes, reference_count
from avatar.files.library.attachments import MAX_FILE


GENERATED_FILE = """(f.origin='generated' OR (f.origin='reference' AND f.sha256!='' AND EXISTS (
    SELECT 1 FROM agent_task_files source JOIN chat_files original ON original.id=source.file_id
    WHERE source.task_id=a.task_id AND source.source_user_id=a.source_user_id
      AND source.message_id=a.message_id AND original.user_id=a.source_user_id
      AND original.sha256=f.sha256 AND original.origin='generated')))"""


def workspace_expired(task):
    deadline = task and task['delivery_mode']!='message' and task['status']=='ended' and task['expires_at']
    return bool(deadline and datetime.fromisoformat(deadline).replace(tzinfo=timezone.utc).timestamp()<=time.time())


class PeerResources:
    """配对消息与独立任务各有共享原件，工具设置仍属于参与者。"""
    def __init__(self,dialogues):
        """复用分身领域服务及现有文件存储，不另建解析器或下载通道。"""
        self.dialogues=dialogues;self.files=dialogues.chat.chat_files;self.database=dialogues.database

    def new_workspace(self,db,user_id,title,*,files=True):
        """创建不继承聊天、文件、工具设置的工作区。"""
        identifier=str(uuid.uuid4());stamp=now()
        avatar=db.execute('SELECT aic FROM avatar_records WHERE user_id=?',(user_id,)).fetchone()
        if not avatar:raise HTTPException(404,'本人分身不存在')
        db.execute("INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at) VALUES(?,?,'peer',?,?,'web',?,?,?)",(identifier,user_id,avatar['aic'],user_id,title,stamp,stamp))
        if files:self.files.directory.workspace(user_id,identifier,create=True)
        return identifier

    def settings_workspace(self,db,user_id,session_id,task_id=None):
        """沿用本人设置和旧私有文件的会话编号。"""
        if task_id:
            task=self.dialogues.owned(db,{'id':user_id},task_id)
            if task['session_id']!=session_id:raise HTTPException(404,'任务不存在')
            if task['delivery_mode']=='message':task_id=None
        table,key=('agent_task_members','task_id') if task_id else ('agent_session_members','session_id')
        identifier=task_id or session_id
        member=db.execute(f'SELECT resource_conversation_id FROM {table} WHERE {key}=? AND user_id=?',(identifier,user_id)).fetchone()
        if not member:raise HTTPException(404,'分身会话不存在')
        if member['resource_conversation_id']:
            return member['resource_conversation_id']
        if task_id and task['status']=='ended':
            raise HTTPException(409,'任务已结束，无法创建工作区')
        workspace=self.new_workspace(db,user_id,'分身任务设置' if task_id else '分身间会话设置',files=False)
        db.execute(f'UPDATE {table} SET resource_conversation_id=? WHERE {key}=? AND user_id=?',(workspace,identifier,user_id))
        return workspace

    @staticmethod
    def shared_id(db,session_id,task_id=None):
        row=db.execute('SELECT conversation_id FROM agent_shared_workspaces WHERE scope=? AND scope_id=?',
                       ('task' if task_id else 'session',task_id or session_id)).fetchone()
        return row[0] if row else None

    def workspace(self, db, user_id, session_id, task_id=None):
        task=self.dialogues.owned(db,{'id':user_id},task_id) if task_id else None
        if task and task['session_id']!=session_id:
            raise HTTPException(404,'任务不存在')
        if task and task['delivery_mode']=='message':
            task_id=None
        self.dialogues.member(db,{'id':user_id},session_id,hidden=bool(task_id))
        if task_id and workspace_expired(task):
            raise HTTPException(410,'任务工作区已过期')
        if workspace:=self.shared_id(db,session_id,task_id):
            return workspace
        workspace=self.new_workspace(db,user_id,'分身任务资源' if task_id else '分身间会话资源')
        db.execute('INSERT INTO agent_shared_workspaces(scope,scope_id,conversation_id) VALUES(?,?,?)',
                   ('task' if task_id else 'session',task_id or session_id,workspace))
        self.import_shared_history(db,session_id,task_id,user_id,workspace)
        return workspace

    def import_shared_history(self,db,session_id,task_id,owner,workspace):
        # A task-file link records explicit sharing. Private files without that link stay in their original workspace.
        scope="d.id=? AND d.delivery_mode<>'message'" if task_id else "d.session_id=? AND d.delivery_mode='message'"
        rows=db.execute(f"""SELECT f.*,a.task_id,a.source_user_id,a.source_file_id,a.message_id
            FROM agent_task_files a JOIN agent_dialogues d ON d.id=a.task_id JOIN chat_files f ON f.id=a.file_id
            WHERE {scope} AND f.conversation_id<>? AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
            ORDER BY (f.user_id=a.source_user_id) DESC,f.created_at,f.id""",(task_id or session_id,workspace)).fetchall()
        originals={}
        for file in rows:
            if not self.files.directory.available_in(file['user_id'],file['conversation_id'],file['id'],file['size']):
                continue
            source=file['source_file_id'] or file['sha256'] or file['id']
            key=(source,file['filename'],file['sha256'])
            if key not in originals:
                identifier=str(uuid.uuid5(uuid.UUID(workspace),json.dumps(key)))
                try:
                    self.copy_in(db,file,owner,workspace,identifier,file['origin'],bool(file['referenced']),link=True)
                except (HTTPException,FileNotFoundError) as exc:
                    logging.getLogger(__name__).warning('Legacy shared file import skipped: id=%s type=%s',file['id'],type(exc).__name__)
                    continue
                originals[key]=identifier
            db.execute("""INSERT INTO agent_task_files(task_id,file_id,source_user_id,source_file_id,message_id)
                VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (file['task_id'],originals[key],file['source_user_id'],file['source_file_id'] or file['id'],file['message_id']))

    def copy_in(self,db,row,owner,workspace,identifier,origin,referenced,*,link=False):
        paths=[]
        try:
            for suffix in ('.blob','.jpg'):
                original=self.files.directory.path(row['user_id'],row['conversation_id'],row['id']+suffix)
                if suffix=='.jpg' and not original.is_file():
                    continue
                path=self.files.directory.path(owner,workspace,identifier+suffix,create=True)
                size=row['size'] if suffix=='.blob' else original.stat().st_size
                if suffix=='.jpg' and size>MAX_FILE:
                    raise HTTPException(413,'图片预览过大')
                if link:
                    os.link(original,path,follow_symlinks=False)
                    paths.append(path)
                    digest,md5=original_hashes(path,size)
                else:
                    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                        paths.append(path)
                        digest,md5=original_hashes(original,size,stream)
                if suffix=='.blob':
                    if (row['sha256'] and row['sha256']!=digest) or (row['md5'] and row['md5']!=md5):
                        raise HTTPException(409,'会话文件校验失败，未发送原件')
                    sha=digest
            db.execute("""INSERT INTO chat_files(id,user_id,conversation_id,filename,content_type,size,extracted_text,
                extraction_status,created_at,origin,description,source_label,sha256,referenced,parse_checked,extraction_version,
                animation_metadata,animation_digest)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'分身会话授权文件',?,?,?,?,?,?)""",
                (identifier,owner,workspace,row['filename'],row['content_type'],row['size'],row['extracted_text'],
                 row['extraction_status'],now(),origin,row['description'],sha,int(referenced),row['parse_checked'],
                 row['extraction_version'],row['animation_metadata'],row['animation_digest']))
            return paths
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise

    @staticmethod
    def sync_task_expiry(db,task_id):
        for table in ('chat_files','conversation_diagrams'):
            db.execute(f"""UPDATE {table} f SET expires_at=d.expires_at
                FROM agent_task_members m JOIN agent_dialogues d ON d.id=m.task_id
                JOIN chat_conversations c ON c.id=m.resource_conversation_id AND c.user_id=m.user_id AND c.channel='peer'
                WHERE d.id=? AND f.conversation_id=m.resource_conversation_id AND f.user_id=m.user_id
                    AND f.expires_at IS DISTINCT FROM d.expires_at
                    AND NOT EXISTS(SELECT 1 FROM agent_session_members s WHERE s.resource_conversation_id=c.id)""",(task_id,))
            db.execute(f"""UPDATE {table} f SET expires_at=d.expires_at
                FROM agent_shared_workspaces w JOIN agent_dialogues d ON w.scope='task' AND w.scope_id=d.id
                WHERE d.id=? AND f.conversation_id=w.conversation_id AND f.expires_at IS DISTINCT FROM d.expires_at""",(task_id,))

    def expired_cards(self,db,task_id,user_id):
        row=db.execute("SELECT expired_files FROM agent_task_members WHERE task_id=? AND user_id=? AND hidden=0",
                       (task_id,user_id)).fetchone()
        if row and row[0]!='[]':
            return json.loads(row[0])
        workspace=self.shared_for_task(db,task_id)
        rows=db.execute(f"""SELECT coalesce(a.source_file_id,f.id) AS id,f.filename,f.content_type,f.size,
            a.source_user_id AS sender_user_id,a.message_id,'generated' AS origin,true AS expired
            FROM agent_task_members m JOIN agent_task_files a ON a.task_id=m.task_id JOIN chat_files f ON a.file_id=f.id
            WHERE m.task_id=? AND m.user_id=? AND m.hidden=0 AND {GENERATED_FILE} AND a.message_id IS NOT NULL
                AND f.conversation_id=coalesce(?::text,m.resource_conversation_id)
            ORDER BY f.created_at,f.id""",(task_id,user_id,workspace))
        return [dict(item) for item in rows]

    def release_workspaces(self, db, stack, workspaces):
        for owner, conversation in set(workspaces):
            if not conversation or not db.execute(
                    "SELECT 1 FROM chat_conversations WHERE user_id=? AND id=? AND channel='peer' FOR UPDATE",
                    (owner,conversation)).fetchone():
                continue
            rows = db.execute('SELECT * FROM chat_files WHERE user_id=? AND conversation_id=?',(owner,conversation)).fetchall()
            stack.enter_context(self.files.deleting(db,rows,workspaces=[(owner,conversation)]))
            db.execute('DELETE FROM agent_intents WHERE user_id=? AND conversation_id=?',(owner,conversation))
            # Old task drafts can carry an archived chat message. Keep its content when the file scope goes away.
            db.execute('UPDATE chat_messages SET conversation_id=NULL WHERE conversation_id=?',(conversation,))
            db.execute('DELETE FROM chat_conversations WHERE user_id=? AND id=?',(owner,conversation))

    def release_task_workspaces(self,db,stack,workspaces):
        candidates=[]
        for owner, workspace in set(workspaces):
            if workspace and not db.execute("""SELECT 1 FROM agent_task_members
                WHERE user_id=? AND resource_conversation_id=? AND hidden=0
                UNION ALL SELECT 1 FROM agent_session_members
                WHERE user_id=? AND resource_conversation_id=? AND hidden=0 LIMIT 1""",
                (owner,workspace,owner,workspace)).fetchone():
                candidates.append((owner,workspace))
        self.release_workspaces(db,stack,candidates)

    def release_shared(self,db,stack,scope,scope_id):
        table,key=('agent_task_members','task_id') if scope=='task' else ('agent_session_members','session_id')
        row=db.execute(f"""SELECT c.user_id,c.id FROM agent_shared_workspaces w JOIN chat_conversations c ON c.id=w.conversation_id
            WHERE w.scope=? AND w.scope_id=? AND NOT EXISTS(SELECT 1 FROM {table} WHERE {key}=? AND hidden=0)""",
            (scope,scope_id,scope_id)).fetchone()
        if row:
            self.release_workspaces(db,stack,[(row['user_id'],row['id'])])

    def share(self,db,user,task_id,file_ids,source=None,generated=False,authorize_ids=(),*,attachments=False):
        """Use a single authorized original for both members of this workspace."""
        identifiers=list(dict.fromkeys(file_ids))
        if not generated and len(identifiers)>5:raise HTTPException(422,'一次最多发送五个文件')
        if not identifiers:return []
        task=self.dialogues.owned(db,user,task_id)
        workspace=self.workspace(db,user['id'],task['session_id'],task_id)
        source=source or workspace
        owner=self.files.session(db,user['id'],workspace)['user_id']
        rows=[self.files.owned(db,user['id'],source,identifier) for identifier in identifiers]
        if not (generated or attachments) and any(not row['referenced'] or row['origin']=='attachment' for row in rows):
            raise HTTPException(409,'请先开启文件引用')
        paths=[]
        try:
            for row in rows:
                identifier=row['id']
                if not self.files.directory.available_in(row['user_id'],source,identifier,row['size']):
                    raise HTTPException(410,'发送文件已删除或过期')
                if workspace==source:
                    digest,md5=original_hashes(self.files.directory.path(owner,workspace,identifier+'.blob'),row['size'])
                    if (row['sha256'] and row['sha256']!=digest) or (row['md5'] and row['md5']!=md5):
                        raise HTTPException(409,'会话文件校验失败，未发送原件')
                else:
                    existing=db.execute("""SELECT f.id FROM chat_files f
                        WHERE f.conversation_id=? AND f.filename=? AND f.sha256=? AND f.sha256<>''
                          AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
                        ORDER BY f.created_at,f.id LIMIT 1""",(workspace,row['filename'],row['sha256'])).fetchone()
                    if existing and self.files.directory.available_in(owner,workspace,existing[0],row['size']):
                        original=self.files.directory.path(row['user_id'],source,identifier+'.blob')
                        candidate=self.files.directory.path(owner,workspace,existing[0]+'.blob')
                        digest=original_hashes(original,row['size'])
                        if digest[0]!=row['sha256'] or (row['md5'] and digest[1]!=row['md5']) or original_hashes(candidate,row['size'])!=digest:
                            raise HTTPException(409,'会话原件已改变，请重新选择文件')
                        identifier=existing[0]
                    else:
                        origin='attachment' if attachments else 'reference'
                        identifier=str(uuid.uuid4())
                        paths.extend(self.copy_in(db,row,owner,workspace,identifier,origin,
                                                  attachments or reference_count(db,workspace)<5))
                if row['id'] in authorize_ids:
                    current=db.execute('SELECT referenced FROM chat_files WHERE id=?',(identifier,)).fetchone()
                    if not current['referenced'] and reference_count(db,workspace)<5:
                        db.execute('UPDATE chat_files SET referenced=1 WHERE id=?',(identifier,))
                db.execute('INSERT INTO agent_task_files(task_id,file_id,source_user_id,source_file_id) VALUES(?,?,?,?) '
                           'ON CONFLICT DO NOTHING',(task_id,identifier,user['id'],row['id']))
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return paths

    def selected(self,db,task_id,user_id):
        """发送附件用于本次请求。普通消息仍可继承配对会话的持续引用。"""
        shared=self.shared_for_task(db,task_id)
        if shared:
            if not db.execute("SELECT 1 FROM agent_task_members WHERE task_id=? AND user_id=? AND hidden=0",(task_id,user_id)).fetchone():
                return []
            return [dict(row) for row in db.execute("""SELECT f.* FROM chat_files f
                WHERE f.conversation_id=? AND f.referenced=1 AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
                  AND (f.origin<>'attachment' OR EXISTS(SELECT 1 FROM agent_task_files a WHERE a.file_id=f.id AND a.task_id=?))
                  AND (f.origin<>'generated' OR EXISTS(SELECT 1 FROM agent_task_files a WHERE a.file_id=f.id))
                ORDER BY CASE WHEN f.origin='attachment' THEN 0 ELSE 1 END,f.created_at DESC,f.id LIMIT 5""",(shared,task_id))]
        return [dict(row) for row in db.execute('''SELECT f.* FROM chat_files f JOIN agent_dialogues current ON current.id=?
          WHERE f.user_id=? AND f.referenced=1 AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
          AND EXISTS (SELECT 1 FROM agent_task_files a JOIN agent_dialogues prior ON prior.id=a.task_id
              WHERE a.file_id=f.id AND (a.task_id=current.id OR (f.origin!='attachment' AND current.delivery_mode='message'
                  AND prior.delivery_mode='message' AND prior.session_id=current.session_id)))
          ORDER BY CASE WHEN f.origin='attachment' THEN 0 ELSE 1 END,f.created_at,f.id LIMIT 5''',(task_id,user_id))]

    def shared_for_task(self,db,task_id):
        row=db.execute('SELECT session_id,delivery_mode FROM agent_dialogues WHERE id=?',(task_id,)).fetchone()
        return self.shared_id(db,row['session_id'],None if row['delivery_mode']=='message' else task_id) if row else None

    def model_files(self,db,task_id,owner):
        return [item for item in self.selected(db,task_id,owner) if item.get('sha256')
                and self.files.directory.available_in(item['user_id'],item['conversation_id'],item['id'],item['size'])]

    def session_file_grants(self,db,task,owner,workspace):
        """Only this speaker's shared copies and this turn's saved outputs can be read."""
        current=self.dialogues.owned(db,{'id':owner},task['id'])
        speaker=current['target_user_id'] if current['next_speaker'] else current['user_id']
        if current['status']!='running' or current['revision']!=task['revision'] or speaker!=owner:
            return {}
        storage_owner=self.files.session(db,owner,workspace)['user_id']
        selected=self.model_files(db,task['id'],owner)
        if self.shared_for_task(db,task['id'])==workspace:
            available=db.execute("""SELECT f.id,f.sha256,f.size FROM chat_files f
                WHERE f.conversation_id=? AND f.sha256<>'' AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
                AND (f.origin NOT IN ('attachment','generated') OR EXISTS(
                    SELECT 1 FROM agent_task_files a WHERE a.file_id=f.id AND (a.task_id=? OR a.message_id IS NOT NULL)))""",
                (workspace,task['id']))
            grants={f['id']:f['sha256'] for f in available}
        else:
            grants={item['id']:item['sha256'] for item in selected if item['conversation_id']==workspace}
        held=json.loads(current['agent_checkpoint'] or '{}')
        ids=held.get('file_ids',()) if held.get('speaker_id')==owner else ()
        if ids and held.get('source_revision')==self.dialogues.output_revision(db,current,owner,selected,style=held.get('peer_style')):
            rows=db.execute("""SELECT id,sha256,size FROM chat_files WHERE user_id=? AND conversation_id=?
                AND origin='generated' AND id=ANY(?::text[])
                AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))""",(storage_owner,workspace,list(ids)))
            grants.update((row['id'],row['sha256']) for row in rows if row['sha256'] and
                          self.files.directory.available_in(storage_owner,workspace,row['id'],row['size']))
        return grants

    def validate_file_receipts(self,db,task,owner,workspace,proofs,*,lock=True):
        from avatar.files.conversation.session_files import SessionFiles
        reader=SessionFiles(self.files,owner,workspace,proofs,
                            grants=lambda conn:self.session_file_grants(conn,task,owner,workspace))
        reader.validate_in(db,lock=lock)

    def metadata(self,task_id,user_id,*,expired=False):
        """只返回该查看者已获得的文件名称、下载地址和到期日期。"""

        with self.database.connect() as db:
            if expired:
                return self.expired_cards(db,task_id,user_id)
            shared=self.shared_for_task(db,task_id)
            rows=db.execute(f"""SELECT f.id,f.user_id,f.conversation_id,f.filename,f.content_type,f.size,f.animation_metadata,
                f.expires_at,(SELECT id FROM attachments WHERE id=f.promoted_id AND user_id=?) AS promoted_id,a.source_user_id,a.message_id,
                CASE WHEN {GENERATED_FILE} THEN 'generated' ELSE f.origin END AS display_origin
                FROM chat_files f JOIN agent_task_files a ON a.file_id=f.id
                WHERE a.task_id=? AND ((?::text IS NULL AND f.user_id=?) OR f.conversation_id=?)
                  AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
                ORDER BY f.created_at,f.id""",(user_id,task_id,shared,user_id,shared)).fetchall()
        return [{'id':r['id'],'sender_user_id':r['source_user_id'],'message_id':r['message_id'],'origin':r['display_origin'],'promoted_id':r['promoted_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],**json.loads(r['animation_metadata']),'expires_at':r['expires_at'],'download_url':'/api/conversations/'+r['conversation_id']+'/files/'+r['id']} for r in rows
                if self.files.directory.available_in(r['user_id'],r['conversation_id'],r['id'],r['size'])]

    def cleanup(self):
        """每分钟回收过期原件、预览、解析及授权行，转存的资料副本独立保留。"""
        while True:
            # 分批提交，避免长期停机后的过期原件一起进入事务恢复缓冲。
            with ExitStack() as stack:
                with self.database.connect(write=True) as db:
                    shared=db.execute("""SELECT c.user_id,c.id,d.id AS task_id FROM agent_shared_workspaces w
                        JOIN agent_dialogues d ON w.scope='task' AND w.scope_id=d.id
                        JOIN chat_conversations c ON c.id=w.conversation_id
                        WHERE d.status='ended' AND julianday(d.expires_at)<=julianday('now')
                        ORDER BY d.expires_at LIMIT 10""").fetchall()
                    for scope in shared:
                        for member in db.execute("SELECT user_id FROM agent_task_members WHERE task_id=? AND hidden=0",(scope['task_id'],)):
                            cards=self.expired_cards(db,scope['task_id'],member['user_id'])
                            db.execute("UPDATE agent_task_members SET expired_files=? WHERE task_id=? AND user_id=?",
                                       (json.dumps(cards,ensure_ascii=False),scope['task_id'],member['user_id']))
                    self.release_workspaces(db,stack,[(r['user_id'],r['id']) for r in shared])
                    expired=db.execute("""SELECT m.task_id,m.user_id,m.resource_conversation_id
                        FROM agent_task_members m JOIN agent_dialogues d ON d.id=m.task_id
                        JOIN chat_conversations c ON c.id=m.resource_conversation_id AND c.user_id=m.user_id AND c.channel='peer'
                        WHERE d.delivery_mode<>'message' AND d.status='ended' AND d.expires_at IS NOT NULL
                          AND d.expires_at<=to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                          AND NOT EXISTS(SELECT 1 FROM agent_session_members s WHERE s.resource_conversation_id=c.id)
                          AND NOT EXISTS(SELECT 1 FROM agent_task_members other
                            JOIN agent_dialogues sibling ON sibling.id=other.task_id
                            WHERE other.resource_conversation_id=m.resource_conversation_id
                              AND other.user_id=m.user_id AND other.task_id<>m.task_id
                              AND (sibling.expires_at IS NULL OR julianday(sibling.expires_at)>julianday('now')))
                        ORDER BY d.expires_at LIMIT 10""").fetchall()
                    for scope in expired:
                        cards=self.expired_cards(db,scope['task_id'],scope['user_id'])
                        db.execute('UPDATE agent_task_members SET expired_files=? WHERE task_id=? AND user_id=?',
                                   (json.dumps(cards,ensure_ascii=False),scope['task_id'],scope['user_id']))
                    self.release_workspaces(db,stack,[(row['user_id'],row['resource_conversation_id']) for row in expired])
                    rows=db.execute("SELECT * FROM chat_files WHERE expires_at IS NOT NULL AND julianday(expires_at)<=julianday('now') LIMIT 10").fetchall()
                    stack.enter_context(self.files.deleting(db,rows))
                    db.execute("DELETE FROM conversation_diagrams WHERE expires_at IS NOT NULL AND julianday(expires_at)<=julianday('now')")
            if len(rows)<10 and len(expired)<10 and len(shared)<10:break
        # 异常断电可能留下已写文件而未提交元数据的孤儿；最多七天后同样回收。
        with self.database.connect() as db:
            for path in self.files.directory.glob():
                if path.is_symlink() or path.suffix not in ('.blob','.jpg'):continue
                try:modified=path.stat().st_mtime
                except FileNotFoundError:continue
                if modified>time.time()-7*86400:continue
                try:uuid.UUID(path.stem)
                except ValueError:continue
                if not db.execute('SELECT 1 FROM chat_files WHERE id=?',(path.stem,)).fetchone():
                    path.unlink(missing_ok=True)
                    parent=path.parent
                    if parent.name != 'chat_attachments':
                        owner=parent.parent.parent.name
                        if not db.execute('SELECT 1 FROM chat_conversations WHERE id=? AND user_id=?',(parent.name,owner)).fetchone():
                            self.files.directory.remove_workspace(owner,parent.name)

    def localize(self,db,text,task_id,user_id,message_id,*,expired=False):
        """按本条发言的授权将附件标记、历史下载链接映射到查看者的独立副本。"""
        if '[[attachment:' not in text and '/api/conversations/' not in text:
            return text
        shared=self.shared_for_task(db,task_id)
        rows=db.execute("""SELECT f.id,f.user_id,f.size,f.conversation_id,a.source_file_id FROM agent_task_files a
            JOIN chat_files f ON f.id=a.file_id WHERE a.task_id=? AND a.message_id=?
            AND ((?::text IS NULL AND f.user_id=?) OR f.conversation_id=?)
            AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))""",(task_id,message_id,shared,user_id,shared)).fetchall()
        markers={row['source_file_id']:row['id'] for row in rows if row['source_file_id'] and
                 self.files.directory.available_in(row['user_id'],row['conversation_id'],row['id'],row['size'])}
        if expired:
            markers={file['id']:file['id'] for file in self.expired_cards(db,task_id,user_id)
                     if file['message_id']==message_id}
            text=re.sub(r'!?(?:\[([^\]\n]*)\])\(\s*<?/api/conversations/[\w-]{1,80}/files/[\w-]{1,80}>?\s*\)',
                        lambda match:(match[1]+' · ' if match[1] else '')+'文件已过期',text)
        if '/api/conversations/' in text or any(not row['source_file_id'] for row in rows):
            links=db.execute("""SELECT source.id,source.conversation_id,source.user_id,a.source_user_id,local.id AS local_id,local.user_id AS local_owner,local.size AS local_size,local.conversation_id AS local_conversation
                FROM agent_task_files a JOIN chat_files source ON source.id=a.file_id
                JOIN agent_task_files b ON b.task_id=a.task_id AND b.source_user_id=a.source_user_id AND b.message_id=a.message_id
                JOIN chat_files local ON local.id=b.file_id WHERE a.task_id=? AND a.message_id=?
                AND ((?::text IS NULL AND local.user_id=?) OR local.conversation_id=?)
                AND ((a.source_file_id IS NOT NULL AND b.source_file_id=a.source_file_id) OR
                     (a.source_file_id IS NULL AND source.sha256<>'' AND local.sha256=source.sha256 AND local.filename=source.filename))
                AND (local.expires_at IS NULL OR julianday(local.expires_at)>julianday('now'))""",(task_id,message_id,shared,user_id,shared))
            urls={}
            for row in links:
                if not self.files.directory.available_in(row['local_owner'],row['local_conversation'],row['local_id'],row['local_size']):continue
                if row['user_id']==row['source_user_id']:
                    markers.setdefault(row['id'],row['local_id'])
                urls[(row['conversation_id'],row['id'])]='/api/conversations/'+row['local_conversation']+'/files/'+row['local_id']
            text=re.sub(r'/api/conversations/([\w-]{1,80})/files/([\w-]{1,80})(?![\w-])',
                        lambda match:urls.get(match.groups(),'（附件已过期）' if expired else '（附件不可用）'),text)
        if '[[attachment:' in text:
            lines=text.splitlines(keepends=True)
            fence=''
            for index,line in enumerate(lines):
                edge=re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$',line.rstrip('\r\n'))
                if edge:
                    if not fence:fence=edge[1]
                    elif edge[1].startswith(fence) and not edge[2].strip():fence=''
                elif not fence:
                    lines[index]=re.sub(r'^( {0,3})\[\[attachment:([\w-]{1,80})\]\](?=[ \t]*\r?$)',lambda m:
                        m[1]+('[[attachment:'+markers[m[2]]+']]' if m[2] in markers
                              else '（附件已过期）' if expired else '（附件不可用）'),line)
            text=''.join(lines)
        return text
