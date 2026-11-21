"""管理员仅导入已清洗 JSON；应用进程不抓取或翻译网络题库。"""
import asyncio
from fastapi import APIRouter,Depends,Request,Query,HTTPException
from pydantic import ValidationError
from typing import Literal
from avatar.api.routes import admin
from avatar.knowledge.question_bank import BankInput,CategoryInput,QuestionInput

router=APIRouter(prefix='/api/admin/soul-question-bank',tags=['soul 分类题库'])


@router.get('')
def page(request:Request,category:str=Query(default='',max_length=60),status:Literal['all','active','inactive']='all',search:str=Query(default='',max_length=100),offset:int=Query(default=0,ge=0,le=2147483647),user=Depends(admin)):
    return request.app.state.knowledge.bank.page(user,category,status,search,offset)


@router.post('/import')
async def import_bank(request:Request,user=Depends(admin)):
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>8*1024*1024:raise HTTPException(413,'题库文件最大 8 MiB')
    def validate_and_import():
        try:package=BankInput.model_validate_json(bytes(data))
        except ValidationError:raise HTTPException(422,'题库 JSON 格式无效，请核对版本、类别、题目及字段长度') from None
        return request.app.state.knowledge.bank.import_package(user,package)
    return await asyncio.to_thread(validate_and_import)


@router.post('/categories',status_code=201)
def category_create(body:CategoryInput,request:Request,user=Depends(admin)):
    return request.app.state.knowledge.bank.save_category(user,body,create=True)


@router.put('/categories/{identifier}')
def category_edit(identifier:str,body:CategoryInput,request:Request,user=Depends(admin)):
    if body.id!=identifier:raise HTTPException(422,'类别标识不能变更')
    return request.app.state.knowledge.bank.save_category(user,body)


@router.post('/questions',status_code=201)
def question_create(body:QuestionInput,request:Request,user=Depends(admin)):
    return request.app.state.knowledge.bank.save_question(user,body,create=True)


@router.put('/questions/{identifier}')
def question_edit(identifier:str,body:QuestionInput,request:Request,user=Depends(admin)):
    if body.id!=identifier:raise HTTPException(422,'题目标识不能变更')
    return request.app.state.knowledge.bank.save_question(user,body)
