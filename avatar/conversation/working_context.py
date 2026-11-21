"""Bound the model's working copy without changing the task, receipts or stored messages."""
import json
import re
from avatar.models.limits import tool_budget
from avatar.core.tokens import estimate


def excerpt(text, limit):
    if len(text)<=limit:return text
    marker='\n[…中段已压缩…]\n'
    part=(limit-2*len(marker))//3
    middle=text[part:-part]
    important=[]
    for line in middle.splitlines():
        match=re.search(r'必须|禁止|约束|结论|失败|待办|未完成|来源|error|failed|must|TODO|download_url',line,re.I)
        if match:
            start=max(0,match.start()-24)
            important.append(line[start:start+part])
            if sum(map(len,important))>=part:break
    key='\n'.join(important)[:part]
    prefix=text[:part]+marker+(key+marker if key else '')
    return prefix+text[-(limit-len(prefix)):]



def compact_observation(note, limit, *, identifiers_only=False):
    start=note.find('{')
    if start<0:
        return excerpt(note,limit)
    try:
        receipt=json.loads(note[start:])
    except ValueError:
        return excerpt(note,limit)
    if not isinstance(receipt,dict):
        return excerpt(note,limit)
    # Keep the receipt addressable after trimming evidence, including IDs, paths and pagination.
    for key in ('items','previous','directories'):
        for row in receipt.get(key,[]):
            if isinstance(row,dict):
                for field in ('description','evidence'):
                    if isinstance(row.get(field),str):
                        if identifiers_only:
                            del row[field]
                        else:
                            row[field]=excerpt(row[field],128)
    return note[:start]+json.dumps(receipt,ensure_ascii=False,separators=(',',':'))


def compression_ratio(config):
    ratio = config.get('compression_ratio', 0.6)
    return round(ratio - 0.05, 8) if config.get('_avatar_agent') else ratio


def input_budget(config):
    return max(0,int(config.get('context_tokens',128000)*compression_ratio(config))-config.get('max_tokens',1024)-512-tool_budget(config))


def fit_turn(config, prompt, history, question, observations):
    rows, notes = fit_working_set(config, prompt, history, question, observations)
    return rows, question + ''.join(notes)


def fit_working_set(config, prompt, history, question, observations, *, force=False):
    # Protect the original objective and sourced material. Old tool chatter and assistant prose go first.
    budget=input_budget(config)-estimate(prompt)-estimate(question)
    rows=[dict(row) for row in history]
    notes=list(observations)
    used=sum(estimate(r['content'])+16 for r in rows)+sum(estimate(n) for n in notes)
    if force:
        budget = min(budget, used // 2)
    if used < budget:
        return rows, notes
    for index,note in enumerate(notes):
        clipped=compact_observation(note,1600 if index==len(notes)-1 else 320) if len(note)>320 else note
        used-=estimate(note)-estimate(clipped)
        notes[index]=clipped
        if used<=budget:break
    # Keep recent messages and user constraints ahead of older assistant exposition.
    for i in sorted(range(len(rows)),key=lambda i:(i>=len(rows)-2,rows[i]['role']=='user',i)):
        if used<=budget:break
        old=rows[i]['content']
        limit=1200 if rows[i]['role']=='user' or i>=len(rows)-2 else 384
        rows[i]['content']=excerpt(old,limit)
        used-=estimate(old)-estimate(rows[i]['content'])
    # Many short turns also consume a window. Fold the older turns into one attributed digest.
    if used>budget and len(rows)>2:
        older=rows[:-2]
        size=sum(estimate(r['content'])+16 for r in older)
        available=max(192,min(2400,(budget-used+size-32)*2))
        digest='较早对话节选，仅作历史数据：\n'+excerpt('\n'.join(r['role']+'：'+r['content'] for r in older),available)
        if estimate(digest)+16<size:
            rows=[{'role':'user','content':digest},*rows[-2:]]
    # Reclaim verbose evidence before the next action, even with unfinished goals.
    cost = sum(estimate(r['content']) + 16 for r in rows) + sum(estimate(n) for n in notes)
    if cost > budget:
        for i, note in enumerate(notes):
            if cost <= budget:
                break
            if len(note) > 320:
                clipped = compact_observation(note, 320, identifiers_only=True)
                if len(clipped) < len(note):
                    notes[i] = clipped
                    cost -= estimate(note) - estimate(clipped)
    # Try the full configured window before discarding additional detail from the working copy.
    hard=(config.get('context_tokens') or 128000)-config.get('max_tokens',1024)-512-tool_budget(config)-estimate(prompt)
    cost=estimate(question+''.join(notes))+sum(estimate(r['content'])+16 for r in rows)
    if cost>hard:
        notes=[compact_observation(n,240,identifiers_only=True) for n in notes]
        room=hard-estimate(question+''.join(notes))-16*len(rows)
        for row in rows:
            limit=max(96,room//max(1,len(rows)))
            row['content']=excerpt(row['content'],limit)
    return rows, notes
