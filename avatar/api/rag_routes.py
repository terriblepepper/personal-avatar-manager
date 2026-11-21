from typing import Literal
from fastapi import APIRouter,Depends,Request
from avatar.api.knowledge_routes import owner

router=APIRouter(prefix='/api/me/file-index',tags=['资料 RAG'])

@router.get('')
def status(request:Request,source:Literal['profile','chat']|None=None,user=Depends(owner)):
    return request.app.state.chat.rag.status(user,source)

@router.post('/rebuild')
def rebuild(request:Request,user=Depends(owner)):
    return request.app.state.chat.rag.rebuild(user)

@router.post('/sync')
def sync(request:Request,user=Depends(owner)):
    return request.app.state.chat.rag.sync(user)
