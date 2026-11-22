# 模块说明：根据任务渐进加载技能说明和明确引用的资源，所有路径均受本人技能清单限制。
import json
import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import unquote
from avatar.core.tokens import estimate
from avatar.core.db import dump

TEXT_SUFFIXES = {'.md', '.markdown', '.txt', '.json', '.csv', '.yaml', '.yml', '.toml', '.xml'}


# 职责：从模型的纯 JSON 或 JSON 代码块中读取技能选择结果。
# 输入：text 是选择调用的输出，catalog 是本次目标所有人的可用技能目录。
# 结果与约束：最多选择三个清单内 ID，禁止通过输出指定其他账户或任意文件路径。
def parse_selection(text, catalog):
    if text is None or (isinstance(text,str) and not text.strip()):return [],{}
    value = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip()))
    if value is None:return [],{}
    if not isinstance(value,dict):raise TypeError('技能选择必须为 JSON 对象')
    selected=value.get('skills',[]);requested=value.get('resources',{})
    if selected is None:selected=[]
    if requested is None:requested={}
    allowed={item['id']:set(item['files']) for item in catalog}
    names={}
    for item in catalog:names.setdefault(item['name'].casefold(),[]).append(item['id'])
    # 职责：兼容模型返回目录中的唯一名称或对象，最终仍只允许当前用户目录 ID。
    def identify(item):
        if isinstance(item,dict):item=item.get('id') or item.get('name')
        if not isinstance(item,str):raise ValueError('技能选择不在清单内')
        if item in allowed:return item
        candidates=names.get(item.removeprefix('$').casefold(),[])
        if len(candidates)!=1:raise ValueError('技能选择不在清单内')
        return candidates[0]
    if not isinstance(selected,list) or len(selected)>10 or not isinstance(requested,dict):raise TypeError('技能选择格式无效')
    identifiers=list(dict.fromkeys(identify(item) for item in selected))
    if len(identifiers)>3:raise ValueError('技能选择超过三项')
    resources={}
    for key,paths in requested.items():
        identifier=identify(key)
        if identifier not in identifiers or not isinstance(paths,list) or len(paths)>20 or any(not isinstance(path,str) or path not in allowed[identifier] for path in paths):
            raise ValueError('资源选择不在清单内')
        resources[identifier]=list(dict.fromkeys(resources.get(identifier,[])+paths))
    return identifiers, resources


# 职责：在技能正文预算不足时保留开头及与问题最相关的段落，并明确标记省略。
# 输入：text 是完整说明，question 是当前问题，limit 是剩余字符预算。
# 结果与约束：原件不裁剪；只限制当前上下文，避免任意长说明撑爆请求。
def instruction_excerpt(text, question, limit):
    if len(text)<=limit:
        return text, False
    terms=set(re.findall(r'[a-z0-9_-]{2,}|[\u4e00-\u9fff]{2}',question.lower()))
    head=text[:limit//2]
    chunks=[text[i:i+2000] for i in range(limit//2,len(text),2000)]
    ranked=sorted(enumerate(chunks),key=lambda item:sum(item[1].lower().count(term) for term in terms),reverse=True)
    chosen=sorted(ranked[:max(1,(limit-len(head))//2000)])
    return (head+'\n[中间内容按相关性省略，原件保持完整]\n'+'\n'.join(chunk for _,chunk in chosen))[:limit], True


# 职责：按包内链接递归加载文本资源，同时保留无法读取或超预算的提示。
# 输入：service 提供所有者校验后的资源读取；row 为当前技能，requested 来自受限选择结果。
# 结果与约束：只读当前技能 manifest 内文本；拒绝跨包路径，不执行脚本，不抓取网络链接，原文件不截断。
def load_resources(service, user_id, row, instructions, requested, budget):
    manifest={item['path']:item['size'] for item in json.loads(row['manifest'])}
    pending=[('SKILL.md',instructions)]
    queue=list(requested)
    seen={'SKILL.md'}
    resources, omitted = [], []
    while pending or queue:
        while pending:
            origin, text=pending.pop(0)
            # 同时识别 Markdown 链接和 README.md、references/foo.md 等普通相对文本路径。
            for raw in re.findall(r'\]\(([^\s)]+)\)|(?<![\w/])((?:[\w.-]+/)*[\w.-]+\.(?:md|markdown|txt|json|csv|yaml|yml|toml|xml))(?![\w])',text):
                target=unquote(next(part for part in raw if part)).split('#',1)[0].rstrip('.,;，。；')
                if not target or '://' in target:
                    continue
                queue.append(posixpath.normpath(posixpath.join(posixpath.dirname(origin),target)))
        if not queue:
            break
        path=queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        if path not in manifest or PurePosixPath(path).suffix.lower() not in TEXT_SUFFIXES:
            omitted.append(path)
            continue
        if budget<=0 or manifest[path]>256*1024:
            omitted.append(path+'（超过本轮资源预算）')
            continue
        try:
            content=service.resource(user_id,row['id'],path).read_bytes().decode('utf-8-sig')
        except (UnicodeError,OSError):
            omitted.append(path+'（非 UTF-8 文本或文件不可用）')
            continue
        item={'path':path,'excerpt':content}
        cost=estimate(dump(item)+', ')
        if cost>budget:
            omitted.append(path+'（超过本轮资源预算）')
            continue
        resources.append(item)
        budget-=cost
        pending.append((path,content))
    return resources, omitted, budget
