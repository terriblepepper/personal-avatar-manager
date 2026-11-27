# 模块说明：从用户指定模型平台读取可用目录，保留能力来源；不使用生成调用探测，不把名称推测当作已验证视觉能力。
import asyncio
import json
import httpx
from avatar.models.model_network import pinned_request_url
from fastapi import HTTPException
from avatar.models.diagnostics import check_response, provider_error

from avatar.models.metadata import LABELS, describe, dimension, enrich


# 职责：沿用模型客户端的公网 DNS 校验、固定 IP、原 Host/SNI 及显式代理，不允许重定向携带 Key。
# 输入：config/Key 来自本人输入或同一目标的已保存配置，client 为应用统一模型客户端。
# 结果与约束：GET 目录总超时 20 秒、最多十页/2000 项/4 MiB，不记录凭据或生成计费请求。
async def discover(client,config,key):
    fmt=config['api_format'];base=config['base_url'].rstrip('/')
    url=httpx.URL(base+'/models')
    if url.userinfo or url.query or url.fragment:raise HTTPException(422,'模型目录地址不能含凭据、查询参数或片段')
    client.validate_endpoint(url)
    headers={'Host':url.netloc.decode()}
    if fmt=='anthropic':headers.update({'x-api-key':key,'anthropic-version':'2023-06-01'})
    elif fmt=='gemini':headers['x-goog-api-key']=key
    else:headers['Authorization']='Bearer '+key
    items={};params={};seen=set();total=0;truncated=False
    try:
        async with asyncio.timeout(20), client.session(url, httpx.Timeout(15, connect=8)) as (addresses, session):
            for page in range(10):
                async with session.stream('GET',pinned_request_url(url, addresses),params=params,headers=headers,extensions={'sni_hostname':url.host}) as response:
                    if response.status_code in (404,405):raise HTTPException(422,'该地址未提供模型目录接口，请确认 Base URL 包含正确版本路径；可继续手动填写模型 ID')
                    await check_response(response,key)
                    payload=bytearray()
                    async for chunk in response.aiter_bytes():
                        total+=len(chunk)
                        if total>4*1024*1024:raise HTTPException(502,'模型目录超过 4 MiB 限制')
                        payload.extend(chunk)
                data=json.loads(payload)
                if not isinstance(data,dict):raise ValueError('目录必须为对象')
                if data.get('error'):raise provider_error(200,data['error'],None,key)
                rows=data.get('models' if fmt=='gemini' else 'data')
                if not isinstance(rows,list):raise ValueError('模型列表字段缺失')
                for row in rows:
                    if isinstance(row,dict):
                        item=describe(row)
                        if item:items[item['id']]=item
                    if len(items)>=2000:truncated=True;break
                cursor=data.get('nextPageToken') if fmt=='gemini' else data.get('last_id') if data.get('has_more') else None
                if not cursor or truncated:break
                if not isinstance(cursor,str) or len(cursor)>2000 or cursor in seen:truncated=True;break
                seen.add(cursor);params={'pageToken' if fmt=='gemini' else 'after_id' if fmt=='anthropic' else 'after':cursor}
            else:truncated=True
    except (TimeoutError,httpx.TimeoutException):raise HTTPException(504,'获取模型目录超时，请稍后刷新；未发起回答生成') from None
    except httpx.ConnectError as error:
        from avatar.models.diagnostics import connection_error
        raise connection_error(error,'模型目录') from None
    except httpx.HTTPError:raise HTTPException(502,'无法连接模型目录，请检查 Base URL 和服务器出网网络') from None
    except (ValueError,TypeError,AttributeError):raise HTTPException(502,'模型目录格式无法识别，可手动填写模型 ID') from None
    await enrich(client,config,items)
    return {'models':sorted(items.values(),key=lambda item:(item['category'],item['id'])),'truncated':truncated,
            'notice':'已补充可确认的窗口、输出、维度及输入能力，选择模型后自动填入，未公布参数保留手工设置。类型以接口元数据为准；名称推测不启用多模态；能力自动判定，未确认时按文本处理。目录可见不保证该 Key 具备实际调用权限。'+('目录已截断，请手动输入未显示的模型。' if truncated else '')}
