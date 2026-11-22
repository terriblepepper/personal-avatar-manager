"""配对会话资源授权；使用现有聊天文件格式、解析器、下载及资料转存接口。"""
import json
import os
import re
import time
import uuid
from contextlib import ExitStack
from fastapi import HTTPException
from avatar.core.db import now
from avatar.files.conversation.resources import check_chat_quota, ordinary_chat_bytes, original_hashes
from avatar.files.library.attachments import MAX_FILE


class PeerResources:
    """为每位参与者创建独立工具工作区，只复制明确授权给当前请求的文件。"""
    def __init__(self,dialogues):
        """复用分身领域服务及现有文件存储，不另建解析器或下载通道。"""
        self.dialogues=dialogues;self.files=dialogues.chat.chat_files;self.database=dialogues.database

    def new_workspace(self,db,user_id,title):
        """创建不继承聊天、文件、工具设置的工作区。"""
        identifier=str(uuid.uuid4());stamp=now()
        avatar=db.execute('SELECT aic FROM avatar_records WHERE user_id=?',(user_id,)).fetchone()
        if not avatar:raise HTTPException(404,'本人分身不存在')
        db.execute("INSERT INTO chat_conversations(id,user_id,channel,target_aic,caller_id,platform,title,created_at,updated_at) VALUES(?,?,'peer',?,?,'web',?,?,?)",(identifier,user_id,avatar['aic'],user_id,title,stamp,stamp))
        return identifier

    def workspace(self,db,user_id,session_id,task_id=None):
        """普通交流使用配对工作区，独立任务按任务和本人隔离。"""
        if task_id:
            task=self.dialogues.owned(db,{'id':user_id},task_id)
            if task['session_id']!=session_id:raise HTTPException(404,'任务不存在')
            if task['delivery_mode']=='message':task_id=None
        table,key=('agent_task_members','task_id') if task_id else ('agent_session_members','session_id')
        identifier=task_id or session_id
        member=db.execute(f'SELECT resource_conversation_id FROM {table} WHERE {key}=? AND user_id=?',(identifier,user_id)).fetchone()
        if not member:raise HTTPException(404,'分身会话不存在')
        if member['resource_conversation_id']:return member['resource_conversation_id']
        workspace=self.new_workspace(db,user_id,'分身任务资源' if task_id else '分身间会话资源')
        db.execute(f'UPDATE {table} SET resource_conversation_id=? WHERE {key}=? AND user_id=?',(workspace,identifier,user_id))
        return workspace

    def share(self,db,user,task_id,file_ids,source=None,generated=False,authorize_ids=(),*,attachments=False):
        """事务内校验双方与所有源文件，生成七天临时快照；同内容不重复复制或延长期限。"""
        identifiers=list(dict.fromkeys(file_ids))
        if not generated and len(identifiers)>5:raise HTTPException(422,'一次最多发送五个文件')
        if not identifiers:return []
        task=self.dialogues.owned(db,user,task_id)
        own=self.workspace(db,user['id'],task['session_id'],task_id)
        source=source or own
        rows=[self.files.owned(db,user['id'],source,identifier) for identifier in identifiers]
        if not (generated or attachments) and any(not row['referenced'] or row['origin']=='attachment' for row in rows):raise HTTPException(409,'请先开启文件引用')
        paths=[]
        try:
            for row in rows:
                original=self.files.directory.new(row['user_id'],row['id']+'.blob')
                if not original.is_file():raise HTTPException(410,'发送文件已删除或过期')
                if row['size'] < 0:
                    raise HTTPException(409,'会话原件大小无效')
                digest, md5 = original_hashes(original,row['size'])
                preview_path=original.with_suffix('.jpg')
                if row['sha256'] and row['sha256']!=digest or row.get('md5') and row['md5']!=md5:
                    raise HTTPException(409,'会话文件校验失败，未发送原件')
                for uid in (task['user_id'],task['target_user_id']):
                    destination=self.workspace(db,uid,task['session_id'],task_id)
                    existing=db.execute("""SELECT f.id FROM chat_files f LEFT JOIN agent_task_files a
                        ON a.file_id=f.id AND a.task_id=? WHERE f.conversation_id=? AND f.filename=?
                        AND (f.sha256=? OR f.id=?) AND (f.origin='attachment')=(?=1)
                        AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
                        AND (a.file_id IS NULL OR (a.message_id IS NULL AND a.source_file_id=? AND a.source_user_id=?))
                        ORDER BY CASE WHEN f.id=? THEN 0 ELSE 1 END,f.created_at,f.id LIMIT 1""",
                        (task_id,destination,row['filename'],digest,row['id'],int(attachments),row['id'],user['id'],row['id'])).fetchone()
                    if existing:
                        identifier=existing['id']
                        if identifier!=row['id']:
                            try:stored=original_hashes(self.files.directory.new(uid,identifier+'.blob'),row['size'])
                            except FileNotFoundError:raise HTTPException(410,'接收方已有副本丢失，请删除该引用后重试') from None
                            if stored!=(digest,md5):
                                raise HTTPException(409,'接收方已有副本校验失败，未重复引用')
                    else:
                        origin='attachment' if attachments else 'generated' if generated and row['origin']=='generated' else 'reference'
                        check_chat_quota(db,uid,destination,int(origin!='generated'),row['size'],ordinary_chat_bytes(row))
                        identifier=str(uuid.uuid4());self.files.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
                        for suffix, source_path in (('.blob',original),('.jpg',preview_path)):
                            if suffix=='.jpg' and not source_path.is_file():continue
                            path=self.files.directory.new(uid,identifier+suffix)
                            size = row['size'] if suffix == '.blob' else source_path.stat().st_size
                            if suffix == '.jpg' and size > MAX_FILE:
                                raise HTTPException(413,'图片预览过大')
                            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                                paths.append(path)
                                copied = original_hashes(source_path,size,stream)
                                if suffix == '.blob' and copied != (digest,md5):
                                    raise HTTPException(409,'原件在发送期间发生变化')
                        active=attachments or db.execute("SELECT count(*) FROM chat_files WHERE conversation_id=? AND referenced=1 AND origin!='attachment'",(destination,)).fetchone()[0]<5
                        db.execute('''INSERT INTO chat_files(id,user_id,conversation_id,filename,content_type,size,extracted_text,extraction_status,created_at,origin,description,source_label,sha256,referenced,parse_checked,extraction_version,animation_metadata,animation_digest)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,'分身会话授权文件',?,?,?,?,?,?)''',(identifier,uid,destination,row['filename'],row['content_type'],row['size'],row['extracted_text'],row['extraction_status'],now(),origin,row['description'],digest,int(active),row['parse_checked'],row['extraction_version'],row['animation_metadata'],row['animation_digest']))
                        if row['expires_at']:db.execute('UPDATE chat_files SET expires_at=CASE WHEN julianday(?)<julianday(expires_at) THEN ? ELSE expires_at END WHERE id=?',(row['expires_at'],row['expires_at'],identifier))
                    if attachments:
                        db.execute('UPDATE chat_files SET referenced=1 WHERE id=? AND referenced=0',(identifier,))
                    if row['id'] in authorize_ids:
                        current=db.execute('SELECT referenced FROM chat_files WHERE id=?',(identifier,)).fetchone()
                        if not current['referenced'] and db.execute("SELECT count(*) FROM chat_files WHERE conversation_id=? AND referenced=1 AND origin!='attachment'",(destination,)).fetchone()[0]>=5:
                            raise HTTPException(413,'会话已授权文件达到五份上限，请先关闭不需要的引用')
                        db.execute('UPDATE chat_files SET referenced=1,md5=? WHERE id=?',(md5,identifier))
                    db.execute('INSERT OR IGNORE INTO agent_task_files(task_id,file_id,source_user_id,source_file_id) VALUES(?,?,?,?)',(task_id,identifier,user['id'],row['id']))
        except BaseException:
            for path in paths:path.unlink(missing_ok=True)
            raise
        return paths

    def selected(self,db,task_id,user_id):
        """发送附件用于本次请求。普通消息仍可继承配对会话的持续引用。"""
        return [dict(row) for row in db.execute('''SELECT f.* FROM chat_files f JOIN agent_dialogues current ON current.id=?
          WHERE f.user_id=? AND f.referenced=1 AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))
          AND EXISTS (SELECT 1 FROM agent_task_files a JOIN agent_dialogues prior ON prior.id=a.task_id
              WHERE a.file_id=f.id AND (a.task_id=current.id OR (f.origin!='attachment' AND current.delivery_mode='message'
                  AND prior.delivery_mode='message' AND prior.session_id=current.session_id)))
          ORDER BY CASE WHEN f.origin='attachment' THEN 0 ELSE 1 END,f.created_at,f.id LIMIT 5''',(task_id,user_id))]

    def model_files(self,db,task_id,owner):
        return [item for item in self.selected(db,task_id,owner) if item.get('sha256')
                and self.files.directory.available(owner,item['id'],item['size'])]

    def session_file_grants(self,db,task,owner,workspace):
        """Only this speaker's shared copies and this turn's saved outputs can be read."""
        current=self.dialogues.owned(db,{'id':owner},task['id'])
        speaker=current['target_user_id'] if current['next_speaker'] else current['user_id']
        if current['status']!='running' or current['revision']!=task['revision'] or speaker!=owner:
            return {}
        selected=self.model_files(db,task['id'],owner)
        grants={item['id']:item['sha256'] for item in selected if item['conversation_id']==workspace}
        held=json.loads(current['agent_checkpoint'] or '{}')
        ids=held.get('file_ids',()) if held.get('speaker_id')==owner else ()
        if ids and held.get('source_revision')==self.dialogues.output_revision(db,current,owner,selected):
            rows=db.execute("""SELECT id,sha256,size FROM chat_files WHERE user_id=? AND conversation_id=?
                AND origin='generated' AND id=ANY(?::text[])
                AND (expires_at IS NULL OR julianday(expires_at)>julianday('now'))""",(owner,workspace,list(ids)))
            grants.update((row['id'],row['sha256']) for row in rows if row['sha256'] and
                          self.files.directory.available(owner,row['id'],row['size']))
        return grants

    def validate_file_receipts(self,db,task,owner,workspace,proofs,*,lock=True):
        from avatar.files.conversation.session_files import SessionFiles
        reader=SessionFiles(self.files,owner,workspace,proofs,
                            grants=lambda conn:self.session_file_grants(conn,task,owner,workspace))
        reader.validate_in(db,lock=lock)

    def metadata(self,task_id,user_id):
        """只返回该查看者已获得的文件名称、下载地址和到期日期。"""
        with self.database.connect() as db:
            rows=db.execute("""SELECT f.id,f.conversation_id,f.filename,f.content_type,f.size,f.animation_metadata,
                f.expires_at,f.promoted_id,a.source_user_id,a.message_id,
                CASE WHEN f.origin='reference' AND f.sha256!='' AND EXISTS (
                    SELECT 1 FROM agent_task_files source JOIN chat_files original ON original.id=source.file_id
                    WHERE source.task_id=a.task_id AND source.source_user_id=a.source_user_id
                    AND source.message_id=a.message_id AND original.user_id=a.source_user_id
                    AND original.sha256=f.sha256 AND original.origin='generated'
                ) THEN 'generated' ELSE f.origin END AS display_origin
                FROM chat_files f JOIN agent_task_files a ON a.file_id=f.id
                WHERE a.task_id=? AND f.user_id=? AND julianday(f.expires_at)>julianday('now')
                ORDER BY f.created_at,f.id""",(task_id,user_id)).fetchall()
        return [{'id':r['id'],'sender_user_id':r['source_user_id'],'message_id':r['message_id'],'origin':r['display_origin'],'promoted_id':r['promoted_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],**json.loads(r['animation_metadata']),'expires_at':r['expires_at'],'download_url':'/api/conversations/'+r['conversation_id']+'/files/'+r['id']} for r in rows
                if self.files.directory.available(user_id,r['id'],r['size'])]

    def cleanup(self):
        """每分钟回收过期原件、预览、解析及授权行，转存的资料副本独立保留。"""
        while True:
            # 分批提交，避免长期停机后的过期原件一起进入事务恢复缓冲。
            with ExitStack() as stack:
                with self.database.connect(write=True) as db:
                    rows=db.execute("SELECT * FROM chat_files WHERE expires_at IS NOT NULL AND julianday(expires_at)<=julianday('now') LIMIT 10").fetchall()
                    stack.enter_context(self.files.deleting(db,rows))
                    db.execute("DELETE FROM conversation_diagrams WHERE expires_at IS NOT NULL AND julianday(expires_at)<=julianday('now')")
            if len(rows)<10:break
        # 异常断电可能留下已写文件而未提交元数据的孤儿；最多七天后同样回收。
        with self.database.connect() as db:
            for path in self.files.directory.glob('*'):
                if path.suffix not in ('.blob','.jpg'):continue
                try:modified=path.stat().st_mtime
                except FileNotFoundError:continue
                if modified>time.time()-7*86400:continue
                try:uuid.UUID(path.stem)
                except ValueError:continue
                if not db.execute('SELECT 1 FROM chat_files WHERE id=?',(path.stem,)).fetchone():path.unlink(missing_ok=True)

    def localize(self,db,text,task_id,user_id,message_id):
        """按本条发言的授权将附件标记、历史下载链接映射到查看者的独立副本。"""
        if '[[attachment:' not in text and '/api/conversations/' not in text:
            return text
        rows=db.execute("""SELECT f.id,f.size,f.conversation_id,a.source_file_id FROM agent_task_files a
            JOIN chat_files f ON f.id=a.file_id WHERE a.task_id=? AND a.message_id=? AND f.user_id=?
            AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now'))""",(task_id,message_id,user_id)).fetchall()
        markers={row['source_file_id']:row['id'] for row in rows if row['source_file_id'] and
                 self.files.directory.available(user_id,row['id'],row['size'])}
        if '/api/conversations/' in text or any(not row['source_file_id'] for row in rows):
            links=db.execute("""SELECT source.id,source.conversation_id,source.user_id,a.source_user_id,local.id AS local_id,local.size AS local_size,local.conversation_id AS local_conversation
                FROM agent_task_files a JOIN chat_files source ON source.id=a.file_id
                JOIN agent_task_files b ON b.task_id=a.task_id AND b.source_user_id=a.source_user_id AND b.message_id=a.message_id
                JOIN chat_files local ON local.id=b.file_id AND local.user_id=? WHERE a.task_id=? AND a.message_id=?
                AND ((a.source_file_id IS NOT NULL AND b.source_file_id=a.source_file_id) OR
                     (a.source_file_id IS NULL AND source.sha256<>'' AND local.sha256=source.sha256 AND local.filename=source.filename))
                AND (local.expires_at IS NULL OR julianday(local.expires_at)>julianday('now'))""",(user_id,task_id,message_id))
            urls={}
            for row in links:
                if not self.files.directory.available(user_id,row['local_id'],row['local_size']):continue
                if row['user_id']==row['source_user_id']:
                    markers.setdefault(row['id'],row['local_id'])
                urls[(row['conversation_id'],row['id'])]='/api/conversations/'+row['local_conversation']+'/files/'+row['local_id']
            text=re.sub(r'/api/conversations/([\w-]{1,80})/files/([\w-]{1,80})(?![\w-])',
                        lambda match:urls.get(match.groups(),'（附件不可用）'),text)
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
                        m[1]+('[[attachment:'+markers[m[2]]+']]' if m[2] in markers else '（附件不可用）'),line)
            text=''.join(lines)
        return text
