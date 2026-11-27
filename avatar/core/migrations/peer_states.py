"""Project historical inline peer states once and retain the original message content."""
import re


def legacy_status(text):
    """提取模型明确输出的状态尾段，代码块中的示例不控制任务。"""
    text=text.strip();fence=None;control=None;position=0
    for line in text.splitlines(keepends=True):
        stripped=line.strip();edge=re.match(r'^(`{3,}|~{3,})',stripped)
        if edge:
            token=edge[1]
            if fence is None:fence=token
            elif token[0]==fence[0] and len(token)>=len(fence) and not stripped[len(token):].strip():fence=None
        elif fence is None:
            match=re.fullmatch(r'\[(任务结束|任务完成|等待用户|方案待确认|继续聊天)(?::(initiator|target|both))?\][ \t]*(.*)',stripped)
            if match:control=(position,position+line.index(']')+1,match[1],match[2])
            else:
                # 兼容旧模型将无原因标记紧贴正文末尾的输出。
                match=re.search(r'\[(任务结束|任务完成|等待用户|方案待确认|继续聊天)\]$',line.rstrip())
                if match and position+len(line)>=len(text) and not stripped.startswith(('>','- ','* ','+ ')):control=(position+match.start(),position+match.end(),match[1],None)
        position+=len(line)
    if control is None:return None
    start,end,marker,who=control;detail=text[end:].strip()
    detail=re.sub(r'^原因[：:][ \t]*','',detail)
    state='ended' if marker in ('任务结束','任务完成') else 'awaiting_input' if marker in ('等待用户','方案待确认') else 'queued'
    action = {'ended':'end_task', 'awaiting_input':'wait_for_user', 'queued':'send_message'}[state]
    return text[:start].rstrip(), state, detail, action


def migrate_peer_states(connection):
    claimed = connection.execute("""INSERT INTO data_migrations(name, applied_at)
        VALUES ('peer-structured-states-v1', CURRENT_TIMESTAMP::text)
        ON CONFLICT DO NOTHING RETURNING name""").fetchone()
    if not claimed:
        return
    connection.execute('ALTER TABLE agent_dialogue_messages ADD COLUMN IF NOT EXISTS legacy_display_content text')
    last_id = 0
    while True:
        rows = connection.execute("""SELECT m.id, m.content FROM agent_dialogue_messages m
            JOIN agent_dialogues d ON d.id=m.dialogue_id WHERE m.id>%s AND m.speaker_role='agent'
              AND m.task_action='' AND d.delivery_mode<>'message' AND strpos(m.content,'[')>0
            ORDER BY m.id LIMIT 128""", (last_id,)).fetchall()
        if not rows:
            return
        for message_id, content in rows:
            if projected := legacy_status(content):
                body, state, detail, action = projected
                connection.execute("""UPDATE agent_dialogue_messages SET legacy_display_content=%s,
                    state=%s, state_detail=coalesce(nullif(%s,''),state_detail), task_action=%s WHERE id=%s""",
                    (body, state, detail, action, message_id))
        last_id = rows[-1][0]
