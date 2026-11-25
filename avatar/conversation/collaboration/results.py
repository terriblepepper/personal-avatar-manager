"""Peer protocol reply projection and confirmed submission bodies."""
import re


def reply_status(text,side):
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
    if control is None:return text,'queued','',0
    start,end,marker,who=control;detail=text[end:].strip()
    detail=re.sub(r'^原因[：:][ \t]*','',detail)
    state='ended' if marker in ('任务结束','任务完成') else 'awaiting_input' if marker in ('等待用户','方案待确认') else 'queued'
    required={'initiator':1,'target':2,'both':3}.get(who,1 if side==0 else 2) if state=='awaiting_input' else 0
    return text[:start].rstrip(),state,detail,required


def confirmed_message(job, speaker_id):
    if job.get('message_prepared') and job['next_speaker']==0 and speaker_id==job['user_id']:
        return job['objective']+job['shared_content']
    return None
