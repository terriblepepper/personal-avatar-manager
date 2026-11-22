"""Model-callable public original export; snapshots are published with a successful reply."""
import uuid
from typing import Annotated
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field
from avatar.files.library.catalog import model_animation_metadata


class PublicMaterialRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    file_ids:list[Annotated[str,Field(min_length=1,max_length=80)]]=Field(min_length=1,max_length=5)


SEND_PUBLIC_MATERIAL={'name':'send_public_material','description':'对方明确索取资料原件时，将本人公开资料目录中指定文件作为本轮附件发送；不是摘要或链接。接收方会获得独立、默认已授权的会话副本。仅接受目录里的 file_id，一次最多五份，不发送私密、未保存或其他用户的文件。','parameters':PublicMaterialRequest.model_json_schema()}


class PublicMaterials:
    def __init__(self,chat):
        self.database=chat.service.database

    def catalog(self,owner_id,query=''):
        with self.database.connect() as db:
            rows=db.execute("""SELECT id AS file_id,filename,relative_path,content_type,substr(caption,1,300) AS description,
                animation_metadata FROM attachments WHERE user_id=? AND visibility='public' AND saved_at IS NOT NULL
                ORDER BY created_at DESC,id""",(owner_id,))
            items=[]
            for row in rows:
                item=dict(row)
                item.update(model_animation_metadata(item.pop('animation_metadata'),query))
                items.append(item)
            return items

    def prepare(self,owner_id,arguments,workspace=None,task_id=None):
        request=PublicMaterialRequest.model_validate(arguments)
        outputs=[]
        with self.database.connect() as db:
            storage_owner=self.chat.chat_files.session(db,owner_id,workspace)['user_id'] if workspace else owner_id
            for identifier in dict.fromkeys(request.file_ids):
                row=db.execute("""SELECT id,filename,sha256,size FROM attachments
                    WHERE id=? AND user_id=? AND visibility='public' AND saved_at IS NOT NULL""",(identifier,owner_id)).fetchone()
                if not row:raise HTTPException(404,'指定公开资料不存在或已取消公开')
                existing=db.execute("""SELECT f.id FROM chat_files f WHERE f.user_id=? AND f.conversation_id=?
                    AND f.origin='reference' AND f.source_label='公开资料原件' AND f.filename=? AND f.sha256=?
                    AND (f.expires_at IS NULL OR julianday(f.expires_at)>julianday('now')) AND NOT EXISTS
                    (SELECT 1 FROM agent_task_files a WHERE a.task_id=? AND a.file_id=f.id)
                    ORDER BY f.created_at,f.id LIMIT 1""",(storage_owner,workspace,row['filename'],row['sha256'],task_id)).fetchone() if workspace else None
                if existing and any(item['id']==existing['id'] for item in outputs):
                    existing=None
                outputs.append({'id':existing['id'] if existing else str(uuid.uuid4()),'reuse':bool(existing),
                                'public_material_id':row['id'],'filename':row['filename'],'sha256':row['sha256'],'size':row['size']})
        return outputs

    @staticmethod
    def validate(db,owner_id,item):
        row=db.execute("""SELECT * FROM attachments WHERE id=? AND user_id=? AND visibility='public'
            AND saved_at IS NOT NULL FOR SHARE""",(item['public_material_id'],owner_id)).fetchone()
        if not row or any(row[key]!=item[key] for key in ('sha256','filename','size')):
            raise HTTPException(409,'公开资料或权限已变化，文件未发送，请重新请求')
        return row
