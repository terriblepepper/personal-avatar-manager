# 模块说明：输出可定位的模型故障元数据，不回显 API Key、模型思考或原始响应正文。
import re
import ssl

import httpx
from fastapi import HTTPException

PURPOSES={'segmented_writing':'接续未完成的输出','agent_intent':'会话意图分析','task_planning':'拆分目标并选择所需工具','goal_decision':'目标核对与工具选择','delivery_review':'交付回执核验','html_generation':'HTML 作品生成','material_selection':'资料原件筛选','skill_execution':'技能脚本规划','memory':'记忆压缩','file_embedding':'向量嵌入','file_search':'资料判断/检索','file_read':'资料原文读取','answer':'回答生成'}


def stop_reason(reason, tool_calls=()):
    """Classify the provider's terminal marker independently of visible output."""
    tag = str(reason or '').lower()
    if tag in ('length', 'max_tokens', 'max_output_tokens', 'model_context_window_exceeded'):
        return 'output_limit'
    if tag in ('context_length_exceeded', 'context_window_exceeded', 'input_too_long'):
        return 'context_overflow'
    if tag in ('cancelled', 'canceled', 'aborted'):
        return 'cancelled'
    if tag == 'interrupted':
        return 'interrupted'
    if tag in ('tool_calls', 'tool_use', 'function_call', 'tool_required'):
        return 'tool'
    if tag in ('', 'stop', 'end_turn', 'stop_sequence'):
        return 'tool' if tool_calls else 'stop'
    return 'error'


def check_terminal(reason, usage):
    terminal = stop_reason(reason)
    detail = {'context_overflow':'输入超过上游模型的上下文窗口', 'cancelled':'上游已取消模型响应',
              'interrupted':'上游中断了模型响应'}
    if terminal in detail:
        error = HTTPException(499 if terminal == 'cancelled' else 502, detail[terminal])
        error.stop_reason = terminal
        error.model_usage = usage
        raise error


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
    terminal = stop_reason(tag)
    if terminal == 'output_limit':
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
    error.output_limited=terminal == 'output_limit'
    error.stop_reason=terminal if terminal not in ('stop','tool') else 'error'
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
    if config.get('reasoning_effort'):
        suffix += '；推理强度='+config['reasoning_effort']
    result=HTTPException(error.status_code,str(error.detail)+'（'+suffix+'）')
    result.model_diagnostic=True
    result.retryable=getattr(error,'retryable',False)
    result.request_not_sent=getattr(error,'request_not_sent',False)
    result.delivery_unknown=getattr(error,'delivery_unknown',False)
    result.timeout_reason=getattr(error,'timeout_reason',None)
    result.stream_exhausted=getattr(error,'stream_exhausted',False)
    result.stop_reason=getattr(error,'stop_reason','error')
    result.output_limited=getattr(error,'output_limited',False)
    result.thinking_only=getattr(error,'thinking_only',False)
    result.recovery_attempted=getattr(error,'recovery_attempted',False)
    result.empty_model_output=getattr(error,'empty_model_output',False)
    result.invalid_tool_call=getattr(error,'invalid_tool_call',False)
    result.tool_issue=getattr(error,'tool_issue',None)
    result.tool_count=getattr(error,'tool_count',None)
    result.undisclosed_tools=getattr(error,'undisclosed_tools',None)
    result.model_usage=getattr(error,'model_usage',None)
    return result


# 职责：从响应头提取供应商请求编号，便于按编号联系供应商排查。
def request_id(headers):return headers.get('x-request-id') or headers.get('request-id')


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
    # Providers also return input overflows as generic invalid_argument errors.
    # Inspect their description locally; it may quote user content and must never be logged.
    description = error.get('message')
    overflow = (status in (200,400,413,422) and (
        stop_reason(code) == 'context_overflow' or stop_reason(kind) == 'context_overflow' or
        isinstance(description,str) and re.match(
            r"\s*(?:prompt is too long\b|(?:the )?(?:input|prompt) token count\b[^\n]{0,100}\bexceeds?\b|"
            r"(?:this model['’]s )?maximum context length is \d+ tokens\b)",
            description, re.I)))
    cancelled = status == 499 or any(stop_reason(marker)=='cancelled' for marker in (code, kind, error.get('status')))
    if cancelled:cause='上游已取消模型请求'
    elif overflow:cause='输入超过上游模型的上下文窗口'
    elif status==401:cause='上游拒绝认证，请检查 API Key'
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
    rid=identifier(rid)
    if rid:fields.append('请求编号='+rid)
    result=HTTPException(502,cause+'（'+'；'.join(fields)+'）。系统未自动重试')
    result.stop_reason = 'cancelled' if cancelled else 'context_overflow' if overflow else 'error'
    result.retryable=not cancelled and status in (408,429,500,502,503,504) and code not in ('insufficient_quota','insufficient_balance')
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


def transport_failure(error, service='模型平台', *, read_timeout=None, total_timeout=None):
    if isinstance(error, httpx.ConnectError):
        return connection_error(error, service)
    if isinstance(error, (httpx.ConnectTimeout, httpx.PoolTimeout)):
        return transport_error(504, f'{service}连接超时，请求尚未发送')
    timed_out = isinstance(error, (TimeoutError, httpx.TimeoutException))
    detail = '请求超时' if timed_out else '传输中断'
    reason = None
    if isinstance(error,httpx.ReadTimeout) and read_timeout is not None:
        reason = f'等待上游响应数据超过 {read_timeout:g} 秒'
    elif isinstance(error,TimeoutError) and total_timeout is not None:
        reason = f'本次模型调用达到 {total_timeout:g} 秒总时限'
    result = unconfirmed_response(504 if timed_out else 502,
                                  f'{service}{reason or detail}，无法确认上游处理结果。请求可能已计费，系统未重发')
    result.timeout_reason = reason
    return result
