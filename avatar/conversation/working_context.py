"""Bound the model's working copy without changing the task, receipts or stored messages."""
import json
import re
from avatar.models.limits import tool_budget
from avatar.core.tokens import estimate


def excerpt(text, limit):
    if len(text)<=limit:return text
    marker='\n[…中段已压缩…]\n'
    part=(limit-2*len(marker))//3
    if part <= 0:
        return text[:max(0, limit)]
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
        receipt, end = json.JSONDecoder().raw_decode(note[start:])
    except ValueError:
        return excerpt(note,limit)
    if not isinstance(receipt,dict):
        return excerpt(note,limit)
    # Keep the receipt addressable after trimming evidence, including IDs, paths and pagination.
    for key in ('items','previous','directories'):
        entries = receipt.get(key)
        for row in entries if isinstance(entries, list) else ():
            if isinstance(row,dict):
                for field in ('description','evidence'):
                    if isinstance(row.get(field),str):
                        if identifiers_only:
                            del row[field]
                        else:
                            row[field]=excerpt(row[field],128)
    suffix = note[start+end:]
    return note[:start]+json.dumps(receipt,ensure_ascii=False,separators=(',',':'))+suffix


def compression_ratio(config):
    ratio = config.get('compression_ratio', 0.6)
    return round(ratio - 0.05, 8) if config.get('_avatar_agent') else ratio


def input_budget(config):
    return max(0,int(config.get('context_tokens',128000)*compression_ratio(config))-config.get('max_tokens',1024)-512-tool_budget(config))


def retained_budget(config, prompt, question):
    window = config.get('context_tokens') or 128000
    overhead = estimate(prompt) + estimate(question) + tool_budget(config) + 512
    return max(0, min(int(window * .32), window - config.get('max_tokens', 1024)) - overhead)


def fit_turn(config, prompt, history, question, observations):
    rows, notes = fit_working_set(config, prompt, history, question, observations)
    return rows, question + ''.join(notes)


def fit_working_set(config, prompt, history, question, observations, *, force=False):
    # Protect the original objective and sourced material. Old tool chatter and assistant prose go first.
    floor = retained_budget(config, prompt, question)
    budget = max(0, input_budget(config) - estimate(prompt) - estimate(question))
    floor = min(floor, budget)
    rows=[dict(row) for row in history]
    notes=list(observations)
    used=sum(estimate(r['content'])+16 for r in rows)+sum(estimate(n) for n in notes)
    if force:
        budget = max(floor, min(budget, used * 4 // 5))
    if used < budget:
        return rows, notes
    compacted = list(dict.fromkeys(notes))
    cost = sum(estimate(note) for note in notes) - sum(estimate(note) for note in compacted)
    if used - cost >= floor:
        notes = compacted
        used -= cost
    for index,note in enumerate(notes):
        if used <= budget:
            break
        clipped=compact_observation(note,1600 if index==len(notes)-1 else 320) if len(note)>320 else note
        saved = estimate(note)-estimate(clipped)
        if saved > 0 and used-saved >= floor:
            used -= saved
            notes[index] = clipped
        if used<=budget:break
    # Keep recent messages and user constraints ahead of older assistant exposition.
    for i in sorted(range(len(rows)),key=lambda i:(i>=len(rows)-2,rows[i]['role']=='user',i)):
        if used<=budget:break
        old=rows[i]['content']
        limit=1200 if rows[i]['role']=='user' or i>=len(rows)-2 else 384
        clipped = excerpt(old,limit)
        if used-estimate(old)+estimate(clipped) < floor:
            retained = max(0, floor-used+estimate(old))
            low, high = min(limit, len(old)), len(old)
            while low < high:
                mid = (low + high) // 2
                if estimate(excerpt(old, mid)) >= retained:
                    high = mid
                else:
                    low = mid + 1
            clipped = excerpt(old, low)
        rows[i]['content'] = clipped
        used -= estimate(old)-estimate(clipped)
    # Many short turns also consume a window. Fold the older turns into one attributed digest.
    if used>budget and len(rows)>2:
        older=rows[:-2]
        size=sum(estimate(r['content'])+16 for r in older)
        available=max(192,min(2400,(budget-used+size-32)*2))
        digest='较早对话节选，仅作历史数据：\n'+excerpt('\n'.join(r['role']+'：'+r['content'] for r in older),available)
        if estimate(digest)+16<size and used-size+estimate(digest)+16 >= floor:
            rows=[{'role':'user','content':digest},*rows[-2:]]
    # Reclaim verbose evidence before the next action, even with unfinished goals.
    cost = sum(estimate(r['content']) + 16 for r in rows) + sum(estimate(n) for n in notes)
    if cost > budget:
        for i, note in enumerate(notes):
            if cost <= budget:
                break
            if len(note) > 320:
                clipped = compact_observation(note, 320, identifiers_only=True)
                if len(clipped) < len(note) and cost-estimate(note)+estimate(clipped) >= floor:
                    notes[i] = clipped
                    cost -= estimate(note) - estimate(clipped)
    if cost > budget:
        # A directory can exceed the window on its identifiers alone. Limit the model view,
        # keeping complete records in storage for a later read by ID or cursor.
        row_cost = sum(estimate(row['content']) + 16 for row in rows)
        note_cost = sum(map(estimate, notes))
        note_room = min(note_cost, max(budget // 2, budget - row_cost))
        row_room = budget - note_room
        kept = []
        for row in reversed(rows):
            if row_room <= 16:
                break
            content = bounded_excerpt(row['content'], row_room - 16)
            kept.append({**row, 'content': content})
            row_room -= estimate(content) + 16
        rows = kept[::-1]
        kept = []
        note_room += row_room
        for note in reversed(notes):
            if note_room <= 0:
                break
            view = bounded_excerpt(note, note_room)
            kept.append(view)
            note_room -= estimate(view)
        notes = kept[::-1]
    return rows, notes


def bounded_excerpt(text, tokens):
    """Fit a textual view by UTF-8 bytes, keeping both ends at character boundaries."""
    data = text.encode('utf-8')
    room = tokens * 3
    if len(data) <= room:
        return text
    mark = '\n[…已节选…]\n'.encode()
    if room <= len(mark) + 6:
        return data[:room].decode('utf-8', 'ignore')
    prefix = (room - len(mark)) // 2
    suffix = room - len(mark) - prefix
    return data[:prefix].decode('utf-8', 'ignore') + mark.decode() + data[-suffix:].decode('utf-8', 'ignore')
