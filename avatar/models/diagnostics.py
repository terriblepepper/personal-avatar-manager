# 模块说明：输出可定位的模型故障元数据，不回显 API Key、模型思考或原始响应正文。
import re
import ssl

import httpx
from fastapi import HTTPException

PURPOSES={'agent_intent':'会话意图分析','task_planning':'任务分解','goal_decision':'目标核对与工具选择','material_selection':'资料原件筛选','animation':'动画分镜','skill_execution':'技能脚本规划','memory':'记忆压缩','skill_selection':'自动技能选择','file_embedding':'向量嵌入','file_search':'资料判断/检索','file_read':'资料原文读取','answer':'回答生成'}


def connection_error(error,service='模型平台'):
    current=error;certificate=False
    for _ in range(6):
        if current is None:break
        certificate|=isinstance(current,ssl.SSLCertVerificationError) or 'certificate verify failed' in str(current).lower()
        current=current.__cause__ or current.__context__
    reason='TLS 证书校验失败，请检查服务器 CA、系统时间和 API 域名' if certificate else '已解析的公网节点均无法连接，请检查服务器出网网络和 API 地址'
    result=HTTPException(502,'无法建立'+service+'连接：'+reason+'；未重发 HTTP 请求')
    result.request_not_sent=True
    result.retryable=not certificate
    return result


# 职责：只接受短的标识符，拒绝任意响应文本或可能含凭据的值。
def identifier(value):
    return str(value)[:120] if isinstance(value,(str,int)) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}',str(value)) and not str(value).startswith(('sk-','Bearer')) else ''


# 职责：构造空响应的具体原因，仅输出终止标记和 Token 计数，不把输出预算误称为余额。
def empty_response(fmt,*,reason=None,thinking=False,refusal=False,usage=None,request_id=None,config=None):
    tag=identifier(reason);usage=usage or {}
    if tag.lower() in ('length','max_tokens','max_output_tokens'):
        cause='达到本次生成的 Token 上限，未生成可见正文；这是单次输出预算限制，不代表账户余额不足'
    elif refusal or tag.lower() in ('content_filter','safety','recitation','blocklist','prohibited_content','spii','refusal'):
        cause='上游返回内容拦截或拒绝，未提供可见正文'
    elif thinking:
        cause='上游只返回了思考内容，没有返回可见正文；未展示思考原文，不能据此认定余额不足'
    else:cause='上游结束响应但可见正文为空；未提供足够原因，无法据此判断账户余额'
    fields=['接口='+fmt,'终止原因='+(tag or '未提供')]
    if config:
        fields.extend(label+'='+str(config[name]) for name,label in (('context_tokens','配置窗口'),('max_tokens','本步请求输出预算')) if name in config)
    mapping=(('prompt_tokens','输入'),('input_tokens','输入'),('completion_tokens','输出'),('output_tokens','输出'),('promptTokenCount','输入'),('candidatesTokenCount','输出'),('thoughtsTokenCount','思考'))
    for name,label in mapping:
        value=usage.get(name)
        if isinstance(value,int) and not isinstance(value,bool) and value>=0:fields.append(label+'Token='+str(value))
    detail=usage.get('completion_tokens_details') or {}
    if isinstance(detail,dict) and isinstance(detail.get('reasoning_tokens'),int):fields.append('思考Token='+str(detail['reasoning_tokens']))
    if identifier(request_id):fields.append('请求编号='+identifier(request_id))
    error=HTTPException(502,cause+'（'+'；'.join(fields)+'）');error.empty_model_output=True
    error.thinking_only=bool(thinking and not refusal and tag.lower() in ('stop','end_turn','stop_sequence'))
    error.output_limited=tag.lower() in ('length','max_tokens','max_output_tokens')
    error.invalid_tool_call=bool((config or {}).get('_tools') and not thinking and not refusal
                                 and tag.lower() in ('','stop','end_turn','stop_sequence'))
    number=lambda value:value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None
    incoming=number(usage.get('prompt_tokens',usage.get('input_tokens',usage.get('promptTokenCount'))))
    outgoing=number(usage.get('completion_tokens',usage.get('output_tokens',usage.get('candidatesTokenCount'))))
    total=number(usage.get('total_tokens',usage.get('totalTokenCount')))
    error.model_usage={'input_tokens':incoming,'output_tokens':outgoing,'total_tokens':total if total is not None else incoming+outgoing if incoming is not None and outgoing is not None else None}

    return contextual(error,config or {})


# 职责：为模型异常附上调用步骤和模型 ID，重复封装不会重复添加，敏感正文不进入日志。
def contextual(error,config):
    if getattr(error,'model_diagnostic',False):return error
    purpose=PURPOSES.get(config.get('_call_purpose','answer'),'回答生成')
    suffix='步骤='+purpose
    if identifier(config.get('model')):suffix+='；模型='+identifier(config['model'])
    result=HTTPException(error.status_code,str(error.detail)+'（'+suffix+'）')
    result.model_diagnostic=True
    result.retryable=getattr(error,'retryable',False)
    result.request_not_sent=getattr(error,'request_not_sent',False)
    result.delivery_unknown=getattr(error,'delivery_unknown',False)
    result.output_limited=getattr(error,'output_limited',False)
    result.thinking_only=getattr(error,'thinking_only',False)
    result.recovery_attempted=getattr(error,'recovery_attempted',False)
    result.empty_model_output=getattr(error,'empty_model_output',False)
    result.invalid_tool_call=getattr(error,'invalid_tool_call',False)
    result.tool_issue=getattr(error,'tool_issue',None)
    result.tool_count=getattr(error,'tool_count',None)
    result.model_usage=getattr(error,'model_usage',None)
    return result


# 职责：从响应头提取供应商请求编号，便于按编号联系供应商排查。
def request_id(headers):return headers.get('x-request-id') or headers.get('request-id')


# 职责：展示结构化错误附带的简短说明，去掉凭据，拒绝包含请求正文/思考内容的回显。
def safe_message(value,secret=''):
    if not isinstance(value,str):return ''
    if re.search(r'(?i)(messages|prompt|reasoning_content|request.body)\s*[=:]',value):return '上游说明包含请求内容，已隐藏'
    if secret:value=value.replace(secret,'[密钥已隐藏]')
    value=re.sub(r'(?i)(bearer\s+)[^\s,;]+',r'\1[已隐藏]',value)
    value=re.sub(r'\bsk-[A-Za-z0-9_-]+','[密钥已隐藏]',value)
    return ' '.join(value.split())[:300]


# 职责：读取有界错误对象，仅展示结构化 code/type，不回显可能包含正文或密钥的 error.message。
async def check_response(response,api_key=''):
    if 200<=response.status_code<300:return
    import json
    body=bytearray()
    async for part in response.aiter_bytes():
        body.extend(part[:max(0,16384-len(body))])
        if len(body)>=16384:break
    try:
        data=json.loads(body);error=data.get('error',{}) if isinstance(data,dict) else {}
        if not isinstance(error,dict):error={}
    except (ValueError,TypeError):error={}
    raise provider_error(response.status_code,error,request_id(response.headers),api_key)


# 职责：统一普通 HTTP 错误和 HTTP 200 内嵌错误，保留安全的供应商原因。
def provider_error(status,error,rid=None,api_key=''):
    error=error if isinstance(error,dict) else {}
    code=identifier(error.get('code'));kind=identifier(error.get('type'))
    if status==401:cause='上游拒绝认证，请检查 API Key'
    elif status==403:cause='上游拒绝访问，可能缺少模型权限或受到地区/策略限制'
    elif status==402 or code in ('insufficient_quota','insufficient_balance'):cause='上游明确报告账户配额或余额不足'
    elif status==429:cause='上游限制请求速率或并发；仅凭 HTTP 429 不能认定余额不足'
    elif status in (400,404,422):cause='上游拒绝请求参数、模型或接口路径'
    else:cause='上游报告错误' if status==200 else '上游服务返回非成功状态'
    fields=['HTTP='+str(status)]
    if code:fields.append('code='+code)
    if kind:fields.append('type='+kind)
    param=identifier(error.get('param'))
    if param:fields.append('参数='+param)
    message=safe_message(error.get('message'),api_key) if code or kind or param else ''
    if message:fields.append('上游说明='+message)
    rid=identifier(rid)
    if rid:fields.append('请求编号='+rid)
    result=HTTPException(502,cause+'（'+'；'.join(fields)+'）。系统未自动重试')
    result.retryable=status in (408,429,500,502,503,504) and code not in ('insufficient_quota','insufficient_balance')
    return result


def transport_error(status,detail):
    """Retryable transport failure before sending the upstream request."""
    result=HTTPException(status,detail)
    result.retryable=True
    result.request_not_sent=True
    return result


def unconfirmed_response(status, detail):
    result = HTTPException(status, detail)
    result.delivery_unknown = True
    result.retryable = False
    return result


def transport_failure(error, service='模型平台'):
    if isinstance(error, httpx.ConnectError):
        return connection_error(error, service)
    if isinstance(error, (httpx.ConnectTimeout, httpx.PoolTimeout)):
        return transport_error(504, f'{service}连接超时，请求尚未发送')
    timed_out = isinstance(error, (TimeoutError, httpx.TimeoutException))
    detail = '请求超时' if timed_out else '传输中断'
    return unconfirmed_response(504 if timed_out else 502,
                                f'{service}{detail}，无法确认上游处理结果。请求可能已计费，系统未重发')
