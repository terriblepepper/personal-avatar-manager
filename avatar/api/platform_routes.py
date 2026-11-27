from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Query
from fastapi import HTTPException
from typing import Annotated
from typing import Literal
from wit import __version__ as wit_version
from fastapi.responses import FileResponse
import avatar.accounts.auth as auth
from avatar.core.config import ROOT


router = APIRouter()


# 职责：检查服务是否能访问数据库。
# 输入：request 提供应用数据库依赖。
# 结果与约束：执行 SELECT 1 后返回 ok、实际运行模式和 wit 版本，不检查外部模型或正式平台状态。
@router.get("/health")
def health(request: Request):
    with request.app.state.database.connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok", "mode": request.app.state.settings.provider, "framework": "wit", "framework_version": wit_version}


@router.get('/help/{document}')
def help_document(
    document: Literal['用户操作手册.md', '知识库与soul.md', '资料RAG与嵌入.md'],
    picture: Annotated[str | None, Query(pattern=r'^[a-z0-9-]+\.png$', max_length=80)] = None,
    user=Depends(auth.current_user),
):
    """仅提供固定的用户帮助文档，不公开目录或接受任意文件路径。"""
    path = ROOT / 'docs' / document
    headers = None
    if document == '用户操作手册.md':
        if not path.is_file():
            path = path.with_stem('用户操作手册-purge')
        headers = {'Cache-Control': 'private, no-store'}
    if picture is not None:
        file = path.parent / 'picture' / path.stem / picture
        if document != '用户操作手册.md' or not file.is_file():
            raise HTTPException(404, '手册图片不存在')
        return FileResponse(file, media_type='image/png', headers=headers)
    return FileResponse(path, media_type='text/markdown; charset=utf-8',
                        filename=document, headers=headers)
