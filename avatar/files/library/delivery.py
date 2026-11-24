"""Read-only, owner-scoped snapshots of saved files for delivery in a personal conversation."""
import stat

from fastapi import HTTPException

from avatar.core.execution import authorization, current_generation


class SavedMaterials:
    def __init__(self, attachments, owner):
        self.attachments = attachments
        self.database = attachments.database
        self.owner = owner

    def documents(self, *, public_only=False):
        from avatar.files.library.folders import with_folder_captions
        # saved_at 记录显式保存；上传后继承目录权限的原件已属于本人。
        condition = 'user_id=?'
        if public_only:
            condition += " AND visibility='public' AND saved_at IS NOT NULL"
        params = [self.owner]
        scope = authorization()
        if scope is not None:
            condition += " AND id=ANY(?::text[]) AND created_at::timestamptz<=to_timestamp(?)"
            params.extend((scope.get('materials', []),current_generation.get()['created_at']))
        with self.database.connect() as db:
            rows=db.execute("""SELECT id,user_id,size,visibility,filename,relative_path,caption,content_type,sha256,
                animation_metadata,'profile' AS source FROM attachments WHERE """+condition+
                ' ORDER BY relative_path,filename,id',params)
            return with_folder_captions(db,(row for row in rows if
                self.attachments.directory.available(self.owner,row['id'],row['size'])),self.owner)

    def owned(self, db, identifier):
        scope = authorization()
        if scope is None or identifier in scope.get('materials', []):
            condition = 'id=? AND user_id=?'
            params = [identifier,self.owner]
            if scope is not None:
                condition += " AND created_at::timestamptz<=to_timestamp(?)"
                params.append(current_generation.get()['created_at'])
            row = db.execute('SELECT id,filename,relative_path,size,sha256,content_type,animation_digest,animation_metadata FROM attachments WHERE '+condition,
                             params).fetchone()
            if row:
                return row
        raise HTTPException(404,'资料文件不存在')

    def original_version(self, row):
        # The attachment is immutable and remains the download source; linking it never reads or copies its bytes.
        path = self.attachments.directory.new(self.owner,row['id']+'.blob')
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise HTTPException(404,'资料原件已删除') from None
        if not stat.S_ISREG(info.st_mode) or info.st_size!=row['size']:
            raise HTTPException(409,'资料原件校验失败')
        return row['sha256'], info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def prepare(self, identifier, *, expected_path=None):
        with self.database.connect() as db:
            source = self.owned(db,identifier)
            path=source['relative_path'] or source['filename']
            if expected_path is not None and path!=expected_path:
                raise HTTPException(409,'所选原件目录已改变，请重新查找')
            return {'id':source['id'],'filename':source['filename'],'relative_path':path,'version':self.original_version(source)}

    def attach(self, db, message_id, prepared):
        cards=[]
        for position, item in enumerate(prepared):
            source = self.owned(db,item['id'])
            if (source['filename']!=item['filename'] or (source['relative_path'] or source['filename'])!=item['relative_path']
                    or self.original_version(source)!=item['version']):
                raise HTTPException(409,'资料文件已改变，请重新请求')
            db.execute('INSERT OR IGNORE INTO chat_message_materials(message_id,attachment_id,position) VALUES(?,?,?)',
                       (message_id,source['id'],position))
            cards.append(material_card(source))
        return cards


def material_card(row):
    return {'id':row['id'],'filename':row['filename'],'size':row['size'],'content_type':row['content_type'],
            'origin':'material','promoted_id':row['id'],'download_url':'/api/me/attachments/'+row['id']}


def visible_delivery_reply(text, files):
    if not files and text == '已附上你要的资料原件。':
        return '这条回复当前没有可用附件。需要原件时，请重新向分身索取。'
    return text
