"""Soul 私有素材目录及合并的增量完善队列；不自动绕过手工审核。"""
import json
import uuid
from datetime import datetime,timezone
from pathlib import Path
from fastapi import HTTPException
from avatar.core.db import now,event
from avatar.files.library.folders import validate_path,validate_name,ensure_folders,with_folder_caption


def changed(db,user_id,*,analyse=True):
    stamp=now()
    db.execute('INSERT INTO knowledge_collections VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at',(user_id,stamp))
    if analyse and db.execute('SELECT 1 FROM active_souls WHERE user_id=?',(user_id,)).fetchone():
        db.execute("INSERT INTO knowledge_asset_updates VALUES(?,?,'',?) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,error='',updated_at=excluded.updated_at",(user_id,uuid.uuid4().hex,stamp))
    return stamp


def folders(k,user_id):
    with k.database.connect() as db:
        rows=db.execute('SELECT path,caption FROM knowledge_folders WHERE user_id=? ORDER BY path',(user_id,)).fetchall()
        stamp=db.execute('SELECT updated_at FROM knowledge_collections WHERE user_id=?',(user_id,)).fetchone()
        pending=db.execute('SELECT error FROM knowledge_asset_updates WHERE user_id=?',(user_id,)).fetchone()
    return {'items':[r['path'] for r in rows],'captions':{r['path']:r['caption'] for r in rows},'updated_at':stamp[0] if stamp else '',
            'pending':bool(pending),'error':pending[0] if pending else ''}


def result(k,user_id):
    with k.database.connect() as db:files=[dict(r) for r in db.execute('SELECT * FROM knowledge_assets WHERE user_id=? ORDER BY created_at DESC',(user_id,))]
    tree=folders(k,user_id)
    return {**tree,'files':files,'last_modified_at':tree['updated_at']}


def check(db,user,stamp):
    current=db.execute('SELECT updated_at FROM knowledge_collections WHERE user_id=?',(user['id'],)).fetchone()
    if stamp is not None and stamp!=(current[0] if current else ''):raise HTTPException(409,'知识库已更新，请刷新后重试')


def folder(k,user,body,operation):
    path=validate_path(body.path)
    if operation=='delete' and body.recursive:
        from avatar.files.library.batch import BatchRequest
        return k.batch(user,BatchRequest(action='delete',folders=[path],last_modified_at=body.last_modified_at))
    with k.database.connect(write=True) as db:
        check(db,user,body.last_modified_at)
        existing=db.execute('SELECT 1 FROM knowledge_folders WHERE user_id=? AND path=?',(user['id'],path)).fetchone()
        if operation=='create':
            if existing:raise HTTPException(409,'文件夹已存在')
            ensure_folders(db,user['id'],path,table='knowledge_folders')
            db.execute('UPDATE knowledge_folders SET caption=? WHERE user_id=? AND path=?',(body.caption.strip(),user['id'],path))
        else:
            if not existing:raise HTTPException(404,'文件夹不存在')
            prefix=path+'/'
            rows=db.execute('SELECT * FROM knowledge_assets WHERE user_id=? AND substr(relative_path,1,?)=?',(user['id'],len(prefix),prefix)).fetchall()
            if operation=='delete':
                children=db.execute('SELECT 1 FROM knowledge_folders WHERE user_id=? AND substr(path,1,?)=?',(user['id'],len(prefix),prefix)).fetchone()
                if not body.recursive and (rows or children):raise HTTPException(409,'文件夹非空，请确认删除全部内容')
                for row in rows:
                    if db.execute("SELECT 1 FROM soul_builds b,json_each(b.asset_ids) a WHERE b.user_id=? AND a.value=? AND b.status IN ('queued','running','waiting_user','ready','pending')",(user['id'],row['id'])).fetchone():raise HTTPException(409,'素材正在构建或审核，请先取消对应构建')
                db.execute('DELETE FROM knowledge_assets WHERE user_id=? AND substr(relative_path,1,?)=?',(user['id'],len(prefix),prefix))
                db.execute('DELETE FROM knowledge_folders WHERE user_id=? AND (path=? OR substr(path,1,?)=?)',(user['id'],path,len(prefix),prefix))
            elif body.new_name is not None:
                target=(path.rpartition('/')[0]+'/' if '/' in path else '')+validate_name(body.new_name)
                if target!=path:
                    if db.execute('SELECT 1 FROM knowledge_folders WHERE user_id=? AND path=? UNION ALL SELECT 1 FROM knowledge_assets WHERE user_id=? AND relative_path=?',(user['id'],target,user['id'],target)).fetchone():raise HTTPException(409,'同名条目已存在')
                    names=[r[0] for r in db.execute('SELECT path FROM knowledge_folders WHERE user_id=? AND (path=? OR substr(path,1,?)=?)',(user['id'],path,len(prefix),prefix))]
                    for name in names:validate_path(target+name[len(path):])
                    for row in rows:validate_path(target+row['relative_path'][len(path):])
                    for name in names:db.execute('UPDATE knowledge_folders SET path=? WHERE user_id=? AND path=?',(target+name[len(path):],user['id'],name))
                    db.execute('UPDATE knowledge_assets SET relative_path=?||substr(relative_path,?) WHERE user_id=? AND substr(relative_path,1,?)=?',(target,len(path)+1,user['id'],len(prefix),prefix))
                path=target
            elif body.caption is not None:db.execute('UPDATE knowledge_folders SET caption=? WHERE user_id=? AND path=?',(body.caption.strip(),user['id'],path))
            else:raise HTTPException(422,'请提供名称或说明')
        stamp=changed(db,user['id'],analyse=operation=='edit' and bool(rows))
        event(db,user['id'],user['username'],'管理 Soul 知识库目录',path,actor_id=user['id'])
    if operation=='delete':
        for row in rows:(k.directory/(row['id']+'.blob')).unlink(missing_ok=True)
    return {**result(k,user['id']),'path':path,'last_modified_at':stamp}


def rename(k,user,identifier,body):
    name=validate_name(body.new_name)
    with k.database.connect(write=True) as db:
        check(db,user,body.last_modified_at);row=k.asset(user['id'],identifier,db)
        if Path(name).suffix.casefold()!=Path(row['filename']).suffix.casefold():raise HTTPException(422,'请保留原文件扩展名')
        parent=row['relative_path'].rpartition('/')[0];path=(parent+'/' if parent else '')+name;validate_path(path)
        if db.execute('SELECT 1 FROM knowledge_assets WHERE user_id=? AND id!=? AND relative_path=? UNION ALL SELECT 1 FROM knowledge_folders WHERE user_id=? AND path=?',(user['id'],identifier,path,user['id'],path)).fetchone():raise HTTPException(409,'同名条目已存在')
        db.execute('UPDATE knowledge_assets SET filename=?,relative_path=? WHERE user_id=? AND id=?',(name,path,user['id'],identifier))
        changed(db,user['id'],analyse=False)
    return result(k,user['id'])


def sync_assets(k):
    with k.database.connect() as db:
        requests=[dict(r) for r in db.execute('SELECT q.*,u.username FROM knowledge_asset_updates q JOIN users u ON u.id=q.user_id WHERE u.account_status=\'active\'')]
    for request in requests:
        if (datetime.now(timezone.utc)-datetime.fromisoformat(request['updated_at'])).total_seconds()<3:continue
        with k.database.connect() as db:
            if db.execute("SELECT 1 FROM soul_builds WHERE user_id=? AND status IN ('queued','running','waiting_user','ready','pending')",(request['user_id'],)).fetchone():continue
            active=db.execute('SELECT b.* FROM active_souls a JOIN soul_builds b ON b.id=a.build_id AND b.user_id=a.user_id WHERE a.user_id=?',(request['user_id'],)).fetchone()
            assets=[dict(r) for r in db.execute('SELECT * FROM knowledge_assets WHERE user_id=? ORDER BY created_at,id',(request['user_id'],))]
            captions={r['id']:with_folder_caption(db,r,table='knowledge_folders')['caption'] for r in assets}
        if not active:continue
        base=k.decode(active);ids=[r['id'] for r in assets]
        if set(ids).issubset(base['asset_ids']) and all(base['asset_captions'].get(key)==value for key,value in captions.items()):
            with k.database.connect(write=True) as db:db.execute('DELETE FROM knowledge_asset_updates WHERE user_id=? AND revision=?',(request['user_id'],request['revision']))
            continue
        try:
            k.refine({'id':request['user_id'],'username':request['username']},base['id'],base['document'],base['updated_at'],asset_ids=ids)
        except HTTPException as exc:
            with k.database.connect(write=True) as db:db.execute('UPDATE knowledge_asset_updates SET error=? WHERE user_id=? AND revision=?',(str(exc.detail)[:1000],request['user_id'],request['revision']))
            continue
        with k.database.connect(write=True) as db:db.execute('DELETE FROM knowledge_asset_updates WHERE user_id=? AND revision=?',(request['user_id'],request['revision']))
