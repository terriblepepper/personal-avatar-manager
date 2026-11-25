"""Owned file/folder selection and inheritable library permissions."""
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from avatar.core.db import event
from avatar.core.revisions import ModifiedAt
from avatar.files.library.folders import validate_path, MaterialFolders


class BatchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['public','private','delete']
    ids:list[str]=Field(default_factory=list,max_length=1000)
    folders:list[str]=Field(default_factory=list,max_length=1000)
    last_modified_at:ModifiedAt|None=None


def select(db,user_id,body,*,knowledge=False):
    table='knowledge_assets' if knowledge else 'attachments'
    tree='knowledge_folders' if knowledge else 'material_folders'
    ids=set(body.ids);paths=set(body.folders)
    if not ids and not paths:raise HTTPException(422,'请先选择文件或文件夹')
    for path in paths:validate_path(path)
    all_folders={r[0] for r in db.execute('SELECT path FROM '+tree+' WHERE user_id=?',(user_id,))}
    rows=[dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE user_id=?',(user_id,))]
    if not ids.issubset({r['id'] for r in rows}) or not paths.issubset(all_folders):
        raise HTTPException(404,'所选文件或文件夹不存在，请刷新后重试')
    folders={p for p in all_folders if any(p==root or p.startswith(root+'/') for root in paths)}
    return [r for r in rows if r['id'] in ids or any(r['relative_path'].startswith(p+'/') for p in paths)],folders


def inherited(db,user_id,path):
    settings={r['path']:r['visibility'] for r in db.execute('SELECT path,visibility FROM material_folders WHERE user_id=?',(user_id,))}
    return folder_visibility(settings,path)


def folder_visibility(settings,path):
    while path:
        if settings.get(path) is not None:return settings[path]
        path=path.rpartition('/')[0]
    return 'private'


def refresh_inherited(db,files,user_id,stamp):
    settings={r['path']:r['visibility'] for r in db.execute('SELECT path,visibility FROM material_folders WHERE user_id=?',(user_id,))}
    rows=db.execute('SELECT id,relative_path,visibility FROM attachments WHERE user_id=? AND visibility_override=0',(user_id,)).fetchall()
    db.executemany('UPDATE attachments SET visibility=?,updated_at=? WHERE id=? AND user_id=?',
        [(folder_visibility(settings,r['relative_path'].rpartition('/')[0]),stamp,r['id'],user_id) for r in rows if r['visibility']!=folder_visibility(settings,r['relative_path'].rpartition('/')[0])])


def apply(files,user,body):
    if body.action=='delete':
        result=files.remove(user,last_modified_at=body.last_modified_at,selection=body)
    else:
        with files.database.connect(write=True) as db:
            files.editable(db,user['id'],body.last_modified_at)
            rows,_=select(db,user['id'],body)
            stamp=files.touch(db,user['id'])
            db.executemany('UPDATE material_folders SET visibility=? WHERE user_id=? AND path=?',[(body.action,user['id'],p) for p in set(body.folders)])
            db.executemany('UPDATE attachments SET visibility=?,visibility_override=1,saved_at=coalesce(saved_at,?),updated_at=? WHERE user_id=? AND id=?',
                [(body.action,stamp,stamp,user['id'],i) for i in set(body.ids)])
            refresh_inherited(db,files,user['id'],stamp)
            event(db,user['id'],user['username'],'批量设置资料权限',body.action,actor_id=user['id'])
        result={'last_modified_at':stamp,'changed_count':len(rows)}
    return {**result,**MaterialFolders(files).list(user['id']),'files':files.inventory(user['id'])}
