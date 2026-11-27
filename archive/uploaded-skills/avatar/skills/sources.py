# 模块说明：只从明确允许的 GitHub 公共仓库下载技能归档，不接受任意远程安装地址。
import asyncio
import io
import re
import os
import zipfile
import zlib
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit
import httpx
from fastapi import HTTPException
from avatar.core.network import resolve_public
from avatar.skills.packages import MAX_UPLOAD, read_entries, zip_entries


# 职责：解析公共仓库首页或 tree/分支/子目录链接，限定下载源和安装范围。
# 输入：HTTPS GitHub URL，不接收凭据、查询参数、跳转或越界路径。
# 结果与约束：返回 codeload 地址及目录元组；分支名不支持斜杠，避免误把分支的一部分当目录。
def repository_source(url):
    try:
        value=urlsplit(url.strip())
        parts=[unquote(part) for part in value.path.strip('/').split('/')]
        if (value.scheme!='https' or value.netloc!='github.com' or value.query or value.fragment
                or len(parts)<2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',part) or part in ('.','..') for part in parts[:2])):
            raise ValueError()
        parts[1]=parts[1].removesuffix('.git')
        if not parts[1] or parts[1] in ('.','..'):
            raise ValueError()
        ref, directory='HEAD', ()
        if len(parts)>2:
            if (len(parts)<5 or parts[2]!='tree'
                    or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',parts[3]) or parts[3] in ('.','..')):
                raise ValueError()
            directory=tuple(parts[4:])
            if (len(directory)>28 or any(not part or part in ('.','..') or len(part)>100
                    or re.search(r'[/\\:%\x00-\x1f\x7f]',part) for part in directory)):
                raise ValueError()
            ref=parts[3]
    except ValueError:
        raise HTTPException(422,'请输入公共 GitHub 仓库首页或 /tree/分支/技能目录 链接；含斜杠的分支名请改用提交 SHA 或上传归档') from None
    return 'https://'+os.getenv('AVATAR_GITHUB_ARCHIVE_HOST','codeload.github.com')+'/'+('/'.join(parts[:2]))+'/zip/'+quote(ref,safe=''), directory


# 职责：从 GitHub 单一根目录归档中裁出指定子目录，防止误安装同仓库的其他技能。
# 输入：content 为有大小上限的 ZIP，directory 为 URL 解析后校验过的目录元组。
# 结果与约束：先复用全包路径、链接、重复及解压预算校验；只重打包目标目录，不落地或执行文件。
def directory_archive(content, directory):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files=read_entries(zip_entries(archive))
        roots={PurePosixPath(name).parts[0] for name in files}
        if len(roots)!=1:
            raise ValueError('仓库归档根目录不唯一')
        prefix=PurePosixPath(next(iter(roots)),*directory)
        selected={str(PurePosixPath(name).relative_to(prefix)):body for name,body in files.items()
                  if PurePosixPath(name).is_relative_to(prefix) and PurePosixPath(name)!=prefix}
        if not any(PurePosixPath(name).name.casefold()=='skill.md' for name in selected):
            raise HTTPException(422,'指定目录不存在或未找到 SKILL.md，请检查分支和技能目录；未安装其他目录')
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,body in selected.items():
                archive.writestr(str(PurePosixPath(directory[-1],name)),body)
        return output.getvalue()
    except (zipfile.BadZipFile, zlib.error, RuntimeError, ValueError, OSError, EOFError, NotImplementedError):
        raise HTTPException(422,'仓库归档无效或超限；拒绝越界路径、链接、重复条目及超过 100 MiB/4096 文件的归档') from None


# 职责：固定已校验公网 IP 下载 GitHub ZIP，按需裁出指定技能目录，供上传解析器复用。
# 输入：url 为公共仓库首页或子目录链接；resolver/transport 仅用于隔离测试注入。
# 结果与约束：验证 TLS/Host/SNI，不继承代理、不跟随跳转，超时 60 秒、最多 50 MiB；不存远程凭据或执行代码。
async def download_repository(url, resolver=resolve_public, transport=None):
    archive_url,directory=repository_source(url)
    endpoint=httpx.URL(archive_url)
    ip=await resolver(endpoint.host,443)
    try:
        async with asyncio.timeout(60), httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False,timeout=httpx.Timeout(60,connect=10)) as client:
            async with client.stream('GET',endpoint.copy_with(host=ip),headers={'Host':endpoint.host},extensions={'sni_hostname':endpoint.host}) as response:
                if response.status_code!=200:
                    raise HTTPException(502,f'GitHub 下载失败（HTTP {response.status_code}），请检查公共仓库地址或改为上传 ZIP')
                content=bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>MAX_UPLOAD:
                        raise HTTPException(413,'仓库归档超过 50 MiB，请上传所需技能目录的归档')
        return await asyncio.to_thread(directory_archive,bytes(content),directory) if directory else bytes(content)
    except (httpx.HTTPError, TimeoutError):
        raise HTTPException(502,'无法下载 GitHub 仓库，请检查服务器网络或改为上传 ZIP') from None
