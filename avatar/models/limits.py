# 模块说明：区分模型上下文窗口和输出上限；仅给已核对的模型提供上限，其他模型由用户按供应商声明填写。
import json
import httpx
from fastapi import HTTPException
from avatar.core.tokens import estimate

# 来源及核对日期见操作手册；网关可能施加更低限制，不能因名称相同推断其实际配额。
MODEL_LIMITS = {
    'gpt-4.1': (1047576,32768), 'gpt-4.1-mini': (1047576,32768),
    'gpt-4o': (128000,16384),
    'deepseek-flash': (1000000,384000), 'deepseek-v4-flash': (1000000,384000),
    'deepseek-v4-pro': (1000000,384000),
}


# 职责：返回官方平台已核对的窗口和输出上限，不把自定义网关的同名模型当作官方模型。
def limits(provider,model):
    value=MODEL_LIMITS.get(model) if provider in ('openai','deepseek') else None
    return {'context_tokens':value[0],'output_tokens':value[1]} if value else None


# 职责：校验保存的用户预算；未知模型使用用户声明的窗口，上游仍有最终裁决权。
def validate(config):
    known=limits(config['provider'],config['model'])
    if known and config['context_tokens']>known['context_tokens']:
        raise HTTPException(422,'上下文窗口超过所选模型已公布的上限')
    if known and config['max_tokens']>known['output_tokens']:
        raise HTTPException(422,'单次输出超过所选模型已公布的上限')
    if config['max_tokens']>=config['context_tokens']:
        raise HTTPException(422,'单次输出必须小于上下文窗口，为输入预留空间')


# 职责：在任何模型请求前按保守文字估算预留输出及消息协议开销，避免超出配置窗口后产生付费失败。
def tool_budget(config):
    if not config.get('_tools'):
        return 0
    from avatar.models.tools import configure_tools
    body={}
    configure_tools(config.get('api_format','openai'),body,config['_tools'],required=not config.get('_optional_tools'),
                    batch=bool(config.get('_batch_tools')),native=bool(config.get('_react_tool_calls')))
    return estimate(json.dumps(body,ensure_ascii=False,separators=(',',':')))


def context_window(config):
    return config.get('context_tokens') or (limits(config.get('provider'),config.get('model')) or {}).get('context_tokens',128000)


def history_row_tokens(row):
    cost = estimate(row.get('content') or '') + 16
    for key in ('tool_calls', 'tool_call_id', 'name', 'replay'):
        if row.get(key):
            cost += estimate(json.dumps(row[key], ensure_ascii=False, separators=(',', ':')))
    return cost


def check_context(config,system,history,message):
    window=context_window(config)
    used=tool_budget(config)+estimate(system)+estimate(message or '')+sum(map(history_row_tokens,history))+config.get('max_tokens',1024)+512
    if used>window:
        error = HTTPException(422,'当前输入与输出预算超过模型上下文窗口，请缩短输入、降低输出上限或新建会话')
        error.stop_reason = 'context_overflow'
        raise error
    return window-used


def _generates_html(config):
    return config.get('_call_purpose') == 'html_generation' or any(
        tool['name'] in ('create_html', 'append_html_draft', 'edit_html') for tool in config.get('_tools') or ())


# 按 token 预算分别限制有效内容和 SSE 传输；每个 token 可能单独携带完整的 JSON 事件头。
def response_limit(config,streaming=False):
    wire_allowance = min(64*1024*1024,max(8*1024*1024,config['max_tokens']*2048))
    if _generates_html(config):
        from avatar.core.artifact_limits import MAX_ENVELOPE_BYTES
        # HTML is nested inside JSON tool arguments. Escapes and streaming headers do not count toward file size.
        return MAX_ENVELOPE_BYTES + (wire_allowance if streaming else 0)
    if streaming:
        return wire_allowance
    return min(64*1024*1024,max(2*1024*1024,config['max_tokens']*32))


def generation_deadline(config):
    return 600 if _generates_html(config) else 180


def generation_http_timeout(config):
    read = 600 if _generates_html(config) else 120
    return httpx.Timeout(120, connect=10, read=read)
