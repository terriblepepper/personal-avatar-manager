"""用户资料目录仅保存逻辑路径，不作为宿主文件系统路径使用。"""
import json
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field,StrictBool
from avatar.core.revisions import ModifiedAt
from avatar.core.db import event


def validate_path(path,*,root=False):
    if path=='' and root:return path
    parts=path.split('/')
    if (not path or len(path)>1024 or len(parts)>20 or '\\' in path
            or any(part in ('','.','..') or ':' in part or any(ord(c)<32 or ord(c)==127 for c in part) for part in parts)):
        raise HTTPException(422,'资料目录路径无效')
    return path


def ensure_folders(db,user_id,path,*,table='material_folders'):
    if table not in ('material_folders','knowledge_folders'):raise ValueError('Invalid directory table')
    validate_path(path,root=True)
    parts=path.split('/') if path else []
    existing={row[0] for row in db.execute('SELECT path FROM '+table+' WHERE user_id=?',(user_id,))}
    needed={'/'.join(parts[:i]) for i in range(1,len(parts)+1)}-existing
    if len(existing)+len(needed)>2000:raise HTTPException(413,'资料文件夹最多 2000 个')
    db.executemany('INSERT INTO '+table+'(user_id,path) VALUES(?,?)',[(user_id,item) for item in sorted(needed)])


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


class FolderRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    path:str=Field(min_length=1,max_length=1024)
    last_modified_at:ModifiedAt|None=None
    recursive:StrictBool=False
    caption:str=Field(default='',max_length=2000)


class MaterialFolders:
    def __init__(self,attachments):self.files=attachments;self.database=attachments.database

    def list(self,user_id):
        with self.database.connect() as db:
            rows=db.execute('SELECT path,caption,visibility FROM material_folders WHERE user_id=? ORDER BY path',(user_id,)).fetchall()
            settings={r['path']:r['visibility'] for r in rows}
            return {'quota':self.files.quota(db,user_id), 'items':[r['path'] for r in rows], 'captions':{r['path']:r['caption'] for r in rows},
                    'visibility':{p:folder_visibility(settings,p) for p in settings},
                    'updated_at':self.files.collection_date(user_id,db)}

    def create(self,user,body):
        path=validate_path(body.path)
        with self.database.connect(write=True, account_scope=user['id']) as db:
            self.files.editable(db,user['id'],body.last_modified_at)
            if db.execute('SELECT 1 FROM material_folders WHERE user_id=? AND path=?',(user['id'],path)).fetchone():raise HTTPException(409,'该文件夹已存在')
            ensure_folders(db,user['id'],path)
            db.execute('UPDATE material_folders SET caption=? WHERE user_id=? AND path=?',(body.caption.strip(),user['id'],path))
            modified=self.files.touch(db,user['id'])
            event(db,user['id'],user['username'],'新建资料文件夹',path,actor_id=user['id'])
        return {**self.list(user['id']),'last_modified_at':modified}


class NameRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    new_name:str=Field(min_length=1,max_length=200)
    last_modified_at:ModifiedAt|None=None
    updated_at:ModifiedAt|None=None


class FolderRenameRequest(NameRequest):
    new_name:str|None=Field(default=None,min_length=1,max_length=200)
    caption:str|None=Field(default=None,max_length=2000)
    path:str=Field(min_length=1,max_length=1024)


def validate_name(name):
    name=name.strip()
    validate_path(name)
    if '/' in name:raise HTTPException(422,'名称不能包含目录分隔符')
    return name


def rename_folder(files,user,body):
    if body.new_name is None:
        if body.caption is None:raise HTTPException(422,'请提供名称或目录说明')
        path=validate_path(body.path)
        with files.database.connect(write=True, account_scope=user['id']) as db:
            files.editable(db,user['id'],body.last_modified_at)
            if not db.execute('UPDATE material_folders SET caption=? WHERE user_id=? AND path=?',(body.caption.strip(),user['id'],path)).rowcount:raise HTTPException(404,'文件夹不存在')
            modified=files.touch(db,user['id']);prefix=path+'/'
            db.execute("UPDATE file_rag_documents SET status='queued',error='',generation=generation+1 WHERE user_id=? AND source='profile' AND file_id IN (SELECT id FROM attachments WHERE user_id=? AND substr(relative_path,1,?)=?)",(user['id'],user['id'],len(prefix),prefix))
            event(db,user['id'],user['username'],'修改资料目录说明',path,actor_id=user['id'])
        return {**MaterialFolders(files).list(user['id']),'last_modified_at':modified,'files':files.inventory(user['id'])}
    if body.caption is not None:raise HTTPException(422,'重命名和修改说明请分别保存')
    path=validate_path(body.path);name=validate_name(body.new_name)
    target=(path.rpartition('/')[0]+'/' if '/' in path else '')+name
    with files.database.connect(write=True, account_scope=user['id']) as db:
        files.editable(db,user['id'],body.last_modified_at)
        folders=[row[0] for row in db.execute('SELECT path FROM material_folders WHERE user_id=?',(user['id'],))]
        if path not in folders:raise HTTPException(404,'文件夹不存在')
        if path==target:return {**MaterialFolders(files).list(user['id']),'last_modified_at':files.collection_date(user['id'])}
        if target in folders:raise HTTPException(409,'同名文件夹已存在')
        rows=db.execute('SELECT id,filename,relative_path FROM attachments WHERE user_id=?',(user['id'],)).fetchall()
        if any((row['relative_path'] or row['filename'])==target for row in rows):raise HTTPException(409,'已存在同名文件')
        updates=[(old,target+old[len(path):]) for old in folders if old==path or old.startswith(path+'/')]
        for old,new in updates:validate_path(new)
        file_updates=[(target+row['relative_path'][len(path):],row['id']) for row in rows if row['relative_path'].startswith(path+'/')]
        if any(len(new)>1024 for new,_ in file_updates):raise HTTPException(422,'改名后文件路径过长')
        for old,new in updates:db.execute('UPDATE material_folders SET path=? WHERE user_id=? AND path=?',(new,user['id'],old))
        modified=files.touch(db,user['id'])
        db.executemany('UPDATE attachments SET relative_path=?,updated_at=? WHERE user_id=? AND id=?',[(new,modified,user['id'],identifier) for new,identifier in file_updates])
        refresh_inherited(db,files,user['id'],modified)
        event(db,user['id'],user['username'],'重命名资料文件夹',path+' → '+target,actor_id=user['id'])
    return {**MaterialFolders(files).list(user['id']),'last_modified_at':modified,'path':target,'files':files.inventory(user['id'])}


def renamed_filename(original,name):
    from pathlib import Path
    name=name.strip()
    if not name or len(name)>200 or name in ('.','..') or '/' in name or '\\' in name or any(ord(c)<32 or ord(c)==127 for c in name):
        raise HTTPException(422,'文件名无效')
    suffix=Path(original).suffix
    if Path(name).suffix.lower()!=suffix.lower():raise HTTPException(422,'请保留原文件扩展名')
    return name[:-len(suffix)]+suffix if suffix else name


def ensure_filename_available(db,user_id,target,exclude_id=''):
    if len(target)>1024:raise HTTPException(422,'文件路径过长')
    if db.execute("SELECT 1 FROM attachments WHERE user_id=? AND id!=? AND (CASE WHEN relative_path='' THEN filename ELSE relative_path END)=?",(user_id,exclude_id,target)).fetchone() or db.execute('SELECT 1 FROM material_folders WHERE user_id=? AND path=?',(user_id,target)).fetchone():
        raise HTTPException(409,'同名文件或文件夹已存在')


def rename_file(files,user,identifier,body):
    with files.database.connect(write=True, account_scope=user['id']) as db:
        files.editable(db,user['id'],body.last_modified_at)
        row=db.execute('SELECT * FROM attachments WHERE user_id=? AND id=?',(user['id'],identifier)).fetchone()
        if not row:raise HTTPException(404,'文件不存在')
        if body.updated_at is not None and body.updated_at!=row['updated_at']:raise HTTPException(409,'文件已变更，请刷新后重试')
        name=renamed_filename(row['filename'],body.new_name)
        parent=row['relative_path'].rpartition('/')[0]
        target=(parent+'/' if parent else '')+name
        ensure_filename_available(db,user['id'],target,identifier)
        modified=files.touch(db,user['id'])
        db.execute('UPDATE attachments SET filename=?,relative_path=?,updated_at=? WHERE user_id=? AND id=?',(name,target,modified,user['id'],identifier))
        event(db,user['id'],user['username'],'重命名资料文件',row['filename']+' → '+name,actor_id=user['id'])
    return {'files':files.inventory(user['id']),'last_modified_at':modified}


def with_folder_caption(db,row,*,table='material_folders'):
    return with_folder_captions(db, [row], row['user_id'], table=table)[0]


def with_folder_captions(db, rows, user_id, *, table='material_folders'):
    if table not in ('material_folders', 'knowledge_folders'):
        raise ValueError('Invalid directory table')
    rows = [dict(row) for row in rows]
    parents = [row.get('relative_path', '').rpartition('/')[0] for row in rows]
    ancestors = {}
    wanted = set()
    for parent in set(parents):
        parts = parent.split('/') if parent else []
        ancestors[parent] = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
        wanted.update(ancestors[parent])

    captions = {}
    if wanted:
        captions = dict(db.execute('SELECT path,caption FROM '+table+' WHERE user_id=? '
                                   'AND path IN (SELECT value FROM json_each(?))',
                                   (user_id, json.dumps(sorted(wanted)))))
    for row, parent in zip(rows, parents):
        paths = ancestors[parent]
        row['folder_descriptions'] = [{'path':path, 'description':captions.get(path, '')} for path in paths]
        notes=['目录 '+path+'：'+captions[path] for path in paths if captions.get(path)]
        row['caption']='\n'.join([*notes,row.get('caption','')]).strip()[:8000]
    return rows
