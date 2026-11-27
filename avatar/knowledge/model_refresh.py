"""Queue and publish reindexes of approved Soul input and memory only."""
import uuid
import json
from pathlib import Path

from fastapi import HTTPException

from avatar.core.execution import run_blocking
from avatar.core.db import dump, event, now
from avatar.models.artifact_cache import fingerprint
from avatar.models.embedding import EmbeddingSettings, index_identity, require_embedding
from avatar.files.word_pdfs import read_word_pdf
from avatar.knowledge.everos_storage import project_for_build
from avatar.knowledge.media import document_visuals

ORIGIN = 'model_refresh'


def active(db, user_id):
    return db.execute("""SELECT b.* FROM active_souls a
        JOIN soul_builds b ON b.id=a.build_id AND b.user_id=a.user_id
        JOIN users u ON u.id=a.user_id
        WHERE a.user_id=? AND b.status='approved' AND u.account_status='active'""", (user_id,)).fetchone()


def target(embedding):
    return fingerprint(index_identity(embedding))


def snapshot(source, refresh):
    return {**dict(source), 'origin':ORIGIN, 'parent_build':source['id'], 'status':'queued',
            'stage':'等待重构原有 Soul 索引', 'phase':'draft', 'progress':0,
            'memory_context':'{}', 'memory_fingerprint':'',
            'memory_tree':dump({'source_project':project_for_build(dict(source)), 'refresh':refresh}),
            'error':'', 'reviewer':None, 'reviewed_at':None, 'reason':'', 'updated_at':now()}


def schedule(db, user_id, *, embedding=False, visual=False, manual=False):
    source = active(db,user_id)
    if not source:
        if manual:raise HTTPException(409,'请先启用已获批的 Soul')
        return None
    config = EmbeddingSettings.current(db,user_id)
    if manual:require_embedding(config)
    if not config.get('encrypted_key'):return None
    if visual and not (embedding or manual):
        notes={item.get('id') for item in json.loads(source['notes']) if item.get('visual') is False}
        candidates=db.execute("""SELECT id,filename,mime FROM knowledge_assets WHERE user_id=? AND id IN
            (SELECT value FROM json_each(?))""",(user_id,source['asset_ids']))
        if not any(asset['mime'].startswith('image/') or
                   (asset['id'] not in notes and Path(asset['filename']).suffix.lower() in ('.doc','.docx'))
                   for asset in candidates):
            return None
    refresh = {'target':target(config),'embedding':embedding,'visual':visual,'manual':manual}
    recent = db.execute("""SELECT id,status,memory_tree,parent_build FROM soul_builds WHERE user_id=? AND origin=?
        AND status IN ('queued','running') ORDER BY status='queued' DESC,created_at DESC LIMIT 1""",(user_id,ORIGIN)).fetchone()
    if recent:
        previous = json.loads(recent['memory_tree'])['refresh']
        for flag in ('embedding','visual','manual'):
            refresh[flag] |= previous[flag]
        if recent['status']=='running' and refresh==previous and recent['parent_build']==source['id']:
            return recent['id']
    values = snapshot(source,refresh)
    if recent and recent['status']=='queued':
        identifier = recent['id']
        changes={key:value for key,value in values.items() if key not in ('id','user_id','created_at')}
        db.execute('UPDATE soul_builds SET '+','.join(name+'=?' for name in changes)+' WHERE id=?',
                   (*changes.values(),identifier))
    else:
        identifier = str(uuid.uuid4())
        values.update(id=identifier,created_at=now())
        db.execute('INSERT INTO soul_builds ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
    return identifier


def rebase(db, row):
    """A learning task ahead of this refresh may have published another approved version."""
    refresh = json.loads(row['memory_tree'])['refresh']
    source = active(db,row['user_id'])
    if not source:raise HTTPException(409,'当前 Soul 已停用，未执行重构')
    if source['id']==row['parent_build']:return row
    values=snapshot(source,refresh)
    changes={key:value for key,value in values.items() if key not in ('id','user_id','created_at','status')}
    db.execute('UPDATE soul_builds SET '+','.join(name+'=?' for name in changes)+' WHERE id=?',
               (*changes.values(),row['id']))
    return {**dict(row),**changes}


def _word_has_visuals(path):
    try:
        pdf,vector=read_word_pdf(path)
    except ValueError as error:
        raise HTTPException(422,str(error)) from None
    return bool(document_visuals(pdf,vector=vector))


async def has_visual_input(service,row):
    notes={item['id']:item for item in row['notes'] if item.get('id')}
    for identifier in row['asset_ids']:
        asset=await run_blocking(service.asset,row['user_id'],identifier)
        if asset['mime'].startswith('image/'):return True
        suffix=Path(asset['filename']).suffix.lower()
        if suffix in ('.doc','.docx'):
            known=notes.get(identifier,{}).get('visual')
            if known is True:return True
            if known is None:
                path,_=await run_blocking(service.file,row['user_id'],identifier)
                result=await service.chat.rag.work.execute(row['user_id'],'parse',_word_has_visuals,path)
                if result:return True
    return False


async def rebuild(worker,row,model,key,embedding):
    refresh=row['memory_tree']['refresh']
    if target(embedding)!=refresh['target']:
        raise HTTPException(409,'模型索引配置已变化，已保留当前 Soul')
    if not (refresh['embedding'] or refresh['manual']) and not await has_visual_input(worker.workspace,row):
        await run_blocking(worker.update_build,row,"UPDATE soul_builds SET status='canceled',progress=100,stage='原输入未使用图片，跳过重构',updated_at=? WHERE id=? AND status='running'",(now(),row['id']))
        return
    worker.percent,worker.stage=50,'重构已获批的 Soul 索引'
    async with worker.chat.rag.work.slot(row['user_id']):
        await worker.memory.compile(row,model,key,{},worker=worker)
    await run_blocking(_publish,worker.database,row,refresh)


def _publish(database,row,refresh):
    with database.connect(write=True,account_scope=row['user_id']) as db:
        current=active(db,row['user_id'])
        if not current or current['id']!=row['parent_build']:
            raise HTTPException(409,'当前 Soul 已变化，未替换已生效版本')
        if target(EmbeddingSettings.current(db,row['user_id']))!=refresh['target']:
            raise HTTPException(409,'重构期间模型索引配置已变化，已保留当前 Soul')
        stamp=now()
        if not db.execute("""UPDATE soul_builds SET status='approved',progress=100,stage='索引重构完成，已自动通过',
            reason='沿用已获批的原输入与风格',reviewed_at=?,updated_at=? WHERE id=? AND status='running'""",
            (stamp,stamp,row['id'])).rowcount:return
        db.execute('UPDATE active_souls SET build_id=? WHERE user_id=? AND build_id=?',(row['id'],row['user_id'],row['parent_build']))
        event(db,row['user_id'],'system','Soul 索引重构自动通过',row['id'])
