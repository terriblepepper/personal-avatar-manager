"""持续学习只开放给本人完整 Web 会话，访客与小镇临时入口不能操作。"""
from fastapi import APIRouter,Depends,Request
from pydantic import BaseModel,ConfigDict,StrictBool
from avatar.api.knowledge_routes import owner

router=APIRouter(prefix='/api/conversations',tags=['本人风格学习'])


class LearningSetting(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:StrictBool


@router.get('/{identifier}/style-learning')
def status(identifier:str,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.learning.status(user,identifier)


@router.patch('/{identifier}/style-learning')
def configure(identifier:str,body:LearningSetting,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.learning.configure(user,identifier,body.enabled)


@router.post('/{identifier}/style-learning/learn')
def learn(identifier:str,request:Request,user=Depends(owner)):
    return request.app.state.knowledge.learning.queue(user,identifier)
