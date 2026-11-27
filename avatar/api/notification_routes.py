"""本人消息通知收件箱。"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from avatar.accounts.auth import current_user
from avatar.api.dependencies import PageOffset, PageSearch

router = APIRouter(prefix='/api', tags=['消息通知'])


@router.get('/notifications/unread-count')
def notification_count(request: Request, user=Depends(current_user)):
    return {'unread': request.app.state.dialogues.inbox.unread_count(user['id'])}


@router.get('/notifications')
def notification_inbox(request:Request,offset: PageOffset = 0,unread:bool=False,status:Literal['all','read','unread']='all',category:Literal['all','chat','request','notification','activity']='all',search: PageSearch = '',user=Depends(current_user)):
    return request.app.state.dialogues.inbox.list(user['id'],offset,unread,status,category,search)


# 批量消息参数仅接受正整数 ID，限定百条，拒绝布尔/字符串 ID 与任意额外字段。
class NotificationBatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ids:list[Annotated[int,Field(strict=True,gt=0)]]=Field(min_length=1,max_length=100)
    action:Literal['read','unread','delete']


# 完整登录和 CSRF 沿用全局边界；领域服务在一个事务中验证整批归属。
@router.post('/notifications/delete-all')
def delete_all_notifications(request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.inbox.delete_all(user['id'])


@router.post('/notifications/batch')
def batch_notifications(body:NotificationBatch,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.inbox.change_many(user['id'],body.ids,body.action)

@router.post('/notifications/{identifier}/{action}')
def change_notification(identifier:int,action:str,request:Request,user=Depends(current_user)):
    return request.app.state.dialogues.inbox.change(user['id'],identifier,action)
