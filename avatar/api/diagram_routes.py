"""绘图外部图片代理：用户显式加载，限制公网 HTTPS、资源大小和图片类型，不执行 SVG/脚本。"""
import asyncio
import base64
import io
import os
from typing import Literal
from urllib.parse import urlsplit
import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request
from avatar.accounts.auth import current_user
from avatar.core.network import address, is_public, resolve_public
from avatar.files.conversation.diagrams import save_mermaid
import json, re

router = APIRouter(prefix='/api')
_limit = asyncio.Semaphore(4)


# 只接收 URL，不接受认证头、代理地址、文件路径或数据库语句。
class DiagramImageRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


# 下载连接固定到已验证 IP，保留 TLS/SNI；解码重编码为 PNG，移除元数据、SVG 与主动内容。
async def public_image(url, *, resolver=None, transport=None):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise HTTPException(422,'图表图片 URL 无效') from None
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or port not in (None,443):
        raise HTTPException(422,'图表图片仅允许不含凭据的公网 HTTPS 地址（443 端口）')
    async with _limit, asyncio.timeout(20):
        ip = await (resolver or resolve_public)(parsed.hostname,443)
        if not is_public(address(ip)):
            raise HTTPException(422,'禁止访问本机或内网图片')
        try:
            async with httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False,timeout=10) as client:
                async with client.stream('GET',httpx.URL(url).copy_with(host=ip),headers={'Host':parsed.hostname},extensions={'sni_hostname':parsed.hostname}) as response:
                    if response.status_code != 200:
                        raise HTTPException(502,'图表图片下载失败（HTTP '+str(response.status_code)+'），不跟随重定向')
                    data=bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data)>5*1024*1024:raise HTTPException(413,'图表图片超过 5 MiB')
            def convert():
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        if image.width*image.height>16_000_000:raise HTTPException(413,'图表图片像素过大')
                        output=io.BytesIO();image.convert('RGBA').save(output,format='PNG')
                        if output.tell()>5*1024*1024:raise HTTPException(413,'转换后的图表图片超过 5 MiB')
                        return 'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()
                except (UnidentifiedImageError,OSError,Image.DecompressionBombError):
                    raise HTTPException(422,'图表外部资源必须是可解码的普通图片，不接受 SVG、HTML 或脚本') from None
            return await asyncio.to_thread(convert)
        except (httpx.HTTPError,TimeoutError):
            raise HTTPException(502,'无法获取图表图片，请稍后重试') from None


# 认证和 CSRF 与其他写 API 一致；不接收、转发 Cookie/Token，不写数据库或保存原件。
@router.post('/diagrams/image')
async def diagram_image(body: DiagramImageRequest, user=Depends(current_user)):
    try:
        return {'data_url':await public_image(body.url)}
    except TimeoutError:
        raise HTTPException(504,'图表图片加载超时') from None


# Iconify 仅作为固定公网图标数据源，不加载第三方 JS、CSS 或完整图标包。
class DiagramIconsRequest(BaseModel):
    prefix: str = Field(pattern=r'^[a-z][a-z0-9-]{0,39}$')
    names: list[str] = Field(min_length=1,max_length=32)


# 按需取得少量图标的 SVG 路径数据；最终仍由沙箱清理，不写业务资料或数据库。
@router.post('/diagrams/icons')
async def diagram_icons(body: DiagramIconsRequest,user=Depends(current_user)):
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',name) for name in body.names):
        raise HTTPException(422,'图标名称无效')
    try:
        async with _limit, asyncio.timeout(20):
            host=os.getenv('AVATAR_ICONIFY_HOST', 'api.iconify.design');ip=await resolve_public(host,443)
            async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=10) as client:
                endpoint=httpx.URL('https://'+host+'/'+body.prefix+'.json',params={'icons':','.join(body.names)}).copy_with(host=ip)
                async with client.stream('GET',endpoint,headers={'Host':host},extensions={'sni_hostname':host}) as response:
                    if response.status_code!=200:raise HTTPException(502,'无法加载指定图标，不跟随重定向')
                    data=bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data)>512*1024:raise HTTPException(413,'图标数据过大')
            value=json.loads(data)
            if not isinstance(value,dict) or value.get('prefix')!=body.prefix or not isinstance(value.get('icons'),dict):
                raise HTTPException(502,'图标数据格式无效')
            return value
    except (httpx.HTTPError,TimeoutError,ValueError):
        raise HTTPException(502,'图标服务暂不可用') from None


class DiagramSourcePromoteRequest(BaseModel):
    source: str = Field(min_length=1, max_length=100000)
    caption: str = Field(default='', max_length=2000)
    visibility: Literal['public','private'] = 'private'
    new_name: str | None = Field(default=None, min_length=1, max_length=200)


@router.post('/conversations/{conversation_id}/diagrams/promote')
async def promote_diagram_source(conversation_id: str,body: DiagramSourcePromoteRequest,request: Request,user=Depends(current_user)):
    return await save_mermaid(request.app.state.chat.chat_files,user,conversation_id,body.source,body.caption,body.visibility,body.new_name)
