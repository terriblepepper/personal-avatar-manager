"""Read and validate durable pause records, saved files and unsettled usage."""
import json

from fastapi import HTTPException

from avatar.core.db import dump, now
from avatar.conversation.loop.store import RunStore
from avatar.files.conversation.resources import original_hashes


def pending(db, user_id, parent_id):
    row = db.execute("""SELECT p.document FROM chat_agent_pauses p JOIN chat_conversations c
        ON c.id=p.conversation_id WHERE c.user_id=? AND c.id=?""", (user_id,parent_id)).fetchone()
    return json.loads(row['document']) if row else None


def checked(db, user_id, request):
    task = pending(db,user_id,request.conversation_id)
    if request.agent_control:
        if not task or task['snapshot']['agent_pause_id'] != request.agent_pause_id:
            raise HTTPException(409,'原暂停状态已改变，请刷新会话')
        question = task['snapshot'].get('user_question')
        if request.question_id and (not question or request.question_id != question['id']):
            raise HTTPException(409,'分身提问已改变，请刷新后回答')
        if question and request.agent_control=='continue' and not request.message.strip():
            raise HTTPException(422,'请填写对分身提问的回答')
        if request.agent_control=='retry' and not task['snapshot'].get('stream_retryable'):
            raise HTTPException(409,'该任务需要补充信息才能继续')
    elif task:
        raise HTTPException(409,'分身已暂停，请刷新后继续任务')
    return task


def available_outputs(chat, db, user_id, parent_id, ids):
    if not ids:return []
    files=[dict(row) for row in db.execute("""SELECT id,filename,size,sha256,animation_metadata::jsonb->>'html_mode' AS html_mode FROM chat_files WHERE user_id=? AND
        conversation_id=? AND generated_message_id IS NULL AND origin='generated' AND
        (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP) AND id IN ("""+
        ','.join('?' for _ in ids)+')',(user_id,parent_id,*ids))]
    available=[]
    for item in files:
        path=chat.chat_files.directory.path(user_id,parent_id,item['id']+'.blob')
        expected=item.pop('sha256')
        try:
            digest,_=original_hashes(path,item['size'])
        except FileNotFoundError:
            continue
        if expected and digest!=expected:
            raise HTTPException(409,'已保存作品的原件校验失败')
        available.append({**item,'origin':'generated','download_url':f"/api/conversations/{parent_id}/files/{item['id']}"})
    return available


def settle_run_usage(db,user_id,parent_id,run_id):
    key=(user_id,'chat',parent_id,run_id)
    state=RunStore.inspect_in(db,*key) or {}
    binding=state.get('binding',{})
    if state.get('usage') and binding.get('provider') and binding.get('model'):
        db.execute("""INSERT INTO model_calls(user_id,provider,model,usage,created_at,caller_id,conversation_id)
            VALUES(?,?,?,?,?,?,?)""",(binding['owner'],binding['provider'],binding['model'],dump(state['usage']),
                                     now(),user_id,binding['session_id']))
        RunStore.mark_usage_recorded_for_in(db,*key)


def settle_terminal_usage(db,row,run_id):
    settle_run_usage(db,row['user_id'],row['conversation_id'],run_id)
