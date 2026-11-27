"""One owner notification per upload action; per-file audit records remain intact."""
import json
import re
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field
from avatar.core.db import dump,event,now

UPLOADS={'profile':'上传资料附件','soul':'上传知识库素材'}


def batch_id(request):
    value=request.headers.get('x-upload-batch','')
    if value and not re.fullmatch('[0-9a-f]{32}',value):raise HTTPException(422,'上传批次标识无效')
    return value


def audit_detail(identifier,batch):
    return dump({'upload_batch_id':batch,'file_id':identifier}) if batch else ''


class UploadComplete(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['profile','soul']
    selected:int=Field(ge=1,le=100000)
    skipped:int=Field(default=0,ge=0,le=100000)


def complete(database,user,identifier,body):
    if not re.fullmatch('[0-9a-f]{32}',identifier):raise HTTPException(422,'上传批次标识无效')
    with database.connect(write=True, account_scope=user['id']) as db:
        count=db.execute("""SELECT count(*) FROM audit_events WHERE user_id=? AND action=?
            AND json_extract(CASE WHEN json_valid(detail) = 1 THEN detail ELSE '{}' END,'$.upload_batch_id')=?""",
            (user['id'],UPLOADS[body.kind],identifier)).fetchone()[0]
        skipped=min(body.skipped,max(0,body.selected-count))
        failed=max(0,body.selected-count-skipped)
        content=f'成功上传 {count} 个文件'+(f'，跳过 {skipped} 个' if skipped else '')+(f'，失败或未完成 {failed} 个' if failed else '')
        label='资料文件' if body.kind=='profile' else 'Soul 素材'
        db.execute("""INSERT OR IGNORE INTO agent_notifications
            (user_id,kind,title,content,created_at,dedupe_key,category,target_page)
            VALUES(?,'upload',?,?,?,?, 'notification',?)""",
            (user['id'],label+'上传完成',content,now(),'upload:'+body.kind+':'+identifier,'files' if body.kind=='profile' else 'knowledge'))
    return {'completed':count,'failed':failed,'skipped':skipped}
