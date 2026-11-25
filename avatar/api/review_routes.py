"""统一审核队列与批量决策，沿用管理员依赖及全局 CSRF 边界。"""
from typing import Literal
from fastapi import APIRouter,Depends,Query,Request,HTTPException
from fastapi.responses import FileResponse
from avatar.api.file_previews import word_pdf
import json
from pydantic import BaseModel,ConfigDict,Field,StrictBool
from avatar.api.dependencies import admin
from avatar.profiles.review_queue import ReviewQueue

router=APIRouter(prefix='/api/admin/review-queue',tags=['统一审核'])


# 客户端不能指定审核人或账户所有权；批量只允许资料和 Soul 申请。
class ReviewItem(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['profile','soul']
    id:str=Field(min_length=1,max_length=80)


class ReviewBatch(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    items:list[ReviewItem]=Field(min_length=1,max_length=100)
    approve:StrictBool
    reason:str=Field(default='',max_length=1000)


class ReviewDeletionItem(ReviewItem):
    kind:Literal['profile','teacher_history','soul']


class ReviewDeletion(BaseModel):
    model_config=ConfigDict(extra='forbid')
    items:list[ReviewDeletionItem]=Field(min_length=1,max_length=100)


@router.post('/delete-all')
def dismiss_all(request:Request,user=Depends(admin)):
    return ReviewQueue(request.app.state.service).dismiss_all(user)


@router.post('/delete')
def dismiss(body:ReviewDeletion,request:Request,user=Depends(admin)):
    return ReviewQueue(request.app.state.service).dismiss(user,body.items)


@router.get('')
def reviews(request:Request,category:Literal['all','teacher','create','update','soul']='all',status:Literal['all','pending','approved','rejected']='pending',search:str=Query(default='',max_length=100),offset:int=Query(default=0,ge=0,le=2147483647),user=Depends(admin)):
    return ReviewQueue(request.app.state.service).page(user,category,status,search,offset)


@router.post('/batch')
def decide(body:ReviewBatch,request:Request,user=Depends(admin)):
    result = ReviewQueue(request.app.state.service).decide(user,body.items,body)
    request.app.state.pending_reviews.invalidate()
    return result


@router.get('/{kind}/{identifier}')
def detail(kind:Literal['profile','teacher_history','soul'],identifier:str,request:Request,user=Depends(admin)):
    return ReviewQueue(request.app.state.service).detail(user,kind,identifier)


@router.get('/soul/{identifier}/assets/{asset_id}')
def soul_source(identifier:str,asset_id:str,request:Request,user=Depends(admin),preview_pdf:bool=False):
    def resolve():
        with request.app.state.database.connect() as db:
            row=db.execute('SELECT user_id,asset_ids,status FROM soul_builds WHERE id=?',(identifier,)).fetchone()
        if not row or row['status'] not in ('pending','approved','rejected') or asset_id not in json.loads(row['asset_ids']):
            raise HTTPException(404,'审核素材不存在')
        return request.app.state.knowledge.file(row['user_id'],asset_id)

    if preview_pdf:
        return word_pdf(resolve)
    path,item=resolve()
    return FileResponse(path,media_type=item['mime'],filename=item['filename'])
