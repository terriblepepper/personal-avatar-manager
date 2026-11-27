"""Translate model messages and provider responses across the three supported APIs."""
from dataclasses import dataclass, field
from fastapi import HTTPException

from avatar.models.diagnostics import check_terminal, stop_reason
from avatar.models.effort import apply_effort


# 职责：表示不同模型平台统一后的完整回答结果。
# 输入：text 为可见回答，usage 为输入、输出及总 Token 用量字典。
# 结果与约束：不可变数据对象；未知用量保持 None，不将缺失用量伪造为零。
@dataclass(frozen=True)
class ModelResult:
    text: str
    usage: dict
    tool_calls: tuple = ()
    finish_reason: str | None = None
    material_sources: tuple = ()
    # None = ordinary reply, empty tuple = explicit clarification, otherwise verified originals.
    material_delivery: tuple | None = None
    contact_draft: dict | None = None
    partial_tool_calls: tuple = field(default=(), repr=False)
    replay: dict | None = field(default=None, repr=False)

    @property
    def stop_reason(self):
        return stop_reason(self.finish_reason, self.tool_calls)


# 职责：校验上游报告的 Token 用量是否为有效整数。
# 输入：value 可为缺失或任意上游 JSON 值。
# 结果与约束：只返回非负整数，排除布尔值、负数和其他类型；未知用量返回 None。
def tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# 职责：解析三种模型接口的完整 JSON 回答与用量。
# 输入：fmt 为接口格式，data 为上游 JSON；OpenAI、Anthropic、Gemini 各自读取对应字段。
# 结果与约束：返回 ModelResult，过滤 Gemini 思考片段；空回答报错，用量优先采用上游总数，无总数时仅在输入输出齐全后求和。
def parse_response(fmt, data, *, config=None, request_id=None, api_key=''):
    config = config or {}
    native = config.get('_react_tool_calls', False)
    retain = native or config.get('_retain_turn_fragments', False)
    separator = '' if retain else '\n'
    if data.get('error'):
        from avatar.models.diagnostics import provider_error,contextual
        raise contextual(provider_error(200,data['error'],request_id,api_key),config or {})
    if fmt == "openai":
        openai_message=(data.get('choices') or [{}])[0].get('message') or {}
        text=openai_message.get('content','')
        refused_part=isinstance(text,list) and any(p.get('type')=='refusal' for p in text)
        if isinstance(text, list):
            text = separator.join(p.get("text", "") for p in text if p.get("type") == "text")
        usage = data.get("usage") or {}
        incoming, outgoing = tokens(usage.get("prompt_tokens")), tokens(usage.get("completion_tokens"))
    elif fmt == "anthropic":
        text = separator.join(p.get("text", "") for p in data.get("content",[]) if p.get("type") == "text")
        usage = data.get("usage") or {}
        incoming, outgoing = tokens(usage.get("input_tokens")), tokens(usage.get("output_tokens"))
    else:
        text = separator.join(p["text"] for p in ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts",[]) if 'text' in p and not p.get("thought"))
        usage = data.get("usageMetadata") or {}
        incoming, outgoing = tokens(usage.get("promptTokenCount")), tokens(usage.get("candidatesTokenCount"))
    reported_total = tokens(usage.get("total_tokens", usage.get("totalTokenCount")))
    total = reported_total if reported_total is not None else incoming+outgoing if incoming is not None and outgoing is not None else None
    model_usage = {"input_tokens": incoming, "output_tokens": outgoing, "total_tokens": total}
    reason=((data.get('choices') or [{}])[0].get('finish_reason') if fmt=='openai' else
            data.get('stop_reason') if fmt=='anthropic' else
            (data.get('candidates') or [{}])[0].get('finishReason') or (data.get('promptFeedback') or {}).get('blockReason'))
    calls=()
    terminal = stop_reason(reason)
    check_terminal(reason, model_usage, native=native)
    if terminal == 'output_limit':
        from avatar.models.tools import identify_calls, raw_tool_calls
        try:
            fragments = raw_tool_calls(fmt, data)
            if native:
                fragments = identify_calls(fragments)
            else:
                fragments = [{k: v for k, v in call.items() if k != 'id'} for call in fragments]
            fragments = tuple(fragments)
            if (text is not None and not isinstance(text,str) or len(fragments) > 8 or
                    any(type(f['index']) is not int or f['index'] < 0 or not isinstance(f['name'],str)
                        or not isinstance(f['arguments'], (str,dict)) for f in fragments)):
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            error = HTTPException(502, '模型输出已截断且片段格式无效，本批工具未执行')
            error.stop_reason = terminal
            error.output_limited = True
            error.model_usage = model_usage
            raise error from None
        if retain:
            return ModelResult(text or '', model_usage, finish_reason=reason, partial_tool_calls=fragments)
        if fragments and config.get('_tools'):
            error = HTTPException(502,'模型工具调用达到单次输出上限，本批工具未执行')
            error.stop_reason = terminal
            error.output_limited = True
            error.model_usage = model_usage
            raise error
    # Distinguish a missing action after private reasoning from a malformed proposed action.
    tools = config.get('_tools') or ()
    replay = None
    if tools or retain:
        from avatar.models.tools import invalid_tool_call, parse_native_calls, parse_tools
        try:
            if native:
                calls, replay = parse_native_calls(fmt, data)
                if terminal == 'tool' and not calls:
                    raise invalid_tool_call('上游结束了工具调用，但未返回可执行参数', 0)
            else:
                calls=tuple(parse_tools(fmt,data,tools,required=bool(tools and text and str(text).strip())
                                        and not config.get('_optional_tools'),batch=bool(config.get('_batch_tools'))))
        except HTTPException as error:
            error.model_usage=model_usage
            if terminal == 'output_limit':
                error.output_limited = True
                error.stop_reason = terminal
                error.invalid_tool_call = False
            raise
    if not calls and (not isinstance(text, str) or not text.strip()):
        from avatar.models.diagnostics import empty_response
        if fmt=='openai':
            choice=(data.get('choices') or [{}])[0];message=choice.get('message') or {}
            reason=choice.get('finish_reason');thinking=bool(message.get('reasoning_content'));refusal=bool(message.get('refusal')) or refused_part
        elif fmt=='anthropic':
            reason=data.get('stop_reason');thinking=any(p.get('type')=='thinking' for p in data.get('content',[]));refusal=reason=='refusal'
        else:
            candidate=(data.get('candidates') or [{}])[0];reason=candidate.get('finishReason') or (data.get('promptFeedback') or {}).get('blockReason')
            thinking=any(p.get('thought') for p in (candidate.get('content') or {}).get('parts',[]));refusal=bool((data.get('promptFeedback') or {}).get('blockReason'))
        if fmt=='openai':
            action=message.get('tool_calls') or message.get('function_call')
        elif fmt=='anthropic':
            action=any(p.get('type')=='tool_use' for p in data.get('content',[]))
        else:
            action=any('functionCall' in p for p in (candidate.get('content') or {}).get('parts',[]))
        if (config and config.get('_tools') and not config.get('_optional_tools') and
                not thinking and not refusal and not action and str(reason or '').lower() in ('','stop','end_turn','stop_sequence')):
            try:
                parse_tools(fmt,data,config['_tools'],required=True)
            except HTTPException as error:
                error.model_usage=model_usage
                raise
        raise empty_response(fmt,reason=reason,thinking=thinking and not action,refusal=refusal,usage=usage,request_id=request_id,config=config)
    # 分身可能在下一次完整响应续写，保留两段交界处的空格、换行与代码缩进。
    visible = (text or '') if retain or config.get('_avatar_agent') else (text or '').strip()
    return ModelResult(visible, model_usage, calls, reason, replay=replay)


# 职责：将统一上下文转换为对应平台 URL、请求头和 JSON。
# 输入：config 决定格式与模型，api_key 仅写鉴权头，system/history/message 构成上下文，streaming 控制流式选项。
# 结果与约束：返回格式、端点、请求头和请求体四元组；Gemini 模型名进行路径编码，OpenAI 流式请求启用用量汇报。
def build_request(config, api_key, system, history, message, streaming=True):
    from avatar.models.limits import check_context
    check_context(config,system,history,message)
    fmt = config["api_format"]
    from avatar.models.tools import wire_messages
    transcript = history if message is None else [*history, {"role": "user", "content": message}]
    conversation = wire_messages(fmt, transcript)
    images=config.get('_image_inputs',[])
    audio=config.get('_audio_inputs',[])
    parts_key = 'parts' if fmt == 'gemini' else 'content'
    last_input = None
    if images or audio:
        if fmt in ('anthropic','gemini') and message is not None and not message.strip():
            if not conversation or conversation[-1]['role'] != 'user':
                conversation.append({'role':'user', parts_key:[]})
            last_input = conversation[-1]
        else:
            for row in reversed(conversation):
                if row['role'] != 'user':
                    continue
                parts = row[parts_key]
                if isinstance(parts, str) or any('text' in part for part in parts):
                    last_input = row
                    break
    if audio and (not config.get('audio_enabled') or fmt not in ('openai','gemini')):
        raise HTTPException(422, '当前模型配置或接口不支持音频输入')
    if (images or audio) and last_input and fmt in ('openai','anthropic'):
        parts = last_input['content']
        if isinstance(parts, str):
            parts = [{'type':'text','text':parts}]
        for image in images:
            if fmt=='openai':parts.append({'type':'image_url','image_url':{'url':'data:'+image['mime_type']+';base64,'+image['data']}})
            else:parts.append({'type':'image','source':{'type':'base64','media_type':image['mime_type'],'data':image['data']}})
        parts.extend({'type':'input_audio','input_audio':{'data':item['data'],'format':'wav'}} for item in audio)
        last_input['content']=parts
    base = config["base_url"].rstrip("/")
    if fmt == "openai":
        endpoint = base + "/chat/completions"
        headers = {"Authorization": "Bearer " + api_key}
        limit_field = "max_completion_tokens" if config["provider"] == "openai" else "max_tokens"
        body = {"model": config["model"], "messages": [{"role": "system", "content": system}]+conversation,
                limit_field: config["max_tokens"], "stream": streaming}
        if (config.get('_recover_thinking') or config.get('_brief_visual_parse') or config.get('_call_purpose') == 'agent_intent'
                or (config.get('_tools') and config.get('_call_purpose') in ('file_read','rag_query_construction'))) and not config.get('reasoning_effort') and config['provider']=='custom' and config['model'].split('/')[-1].lower().startswith('qwen'):
            body['enable_thinking']=False
            body['chat_template_kwargs']={'enable_thinking':False}
        if config['provider']=='deepseek':
            from avatar.models.limits import limits
            if limits('deepseek',config['model']):
                # 平台未提供思考预算配置，显式关闭默认思考，避免吞掉小输出预算而没有可见回答。
                body['thinking']={'type':'disabled'}
                if config.get('_file_search'):body['response_format']={'type':'json_object'}
    elif fmt == "anthropic":
        endpoint = base + "/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body = {"model": config["model"], "system": system, "messages": conversation, "max_tokens": config["max_tokens"], "stream": streaming}
    elif fmt == "gemini":
        from urllib.parse import quote
        model = config["model"].removeprefix("models/")
        endpoint = base + "/models/" + quote(model, safe="") + (":streamGenerateContent?alt=sse" if streaming else ":generateContent")
        headers = {"x-goog-api-key": api_key}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": conversation,
                "generationConfig": {"maxOutputTokens": config["max_tokens"]}}
        if images and last_input:last_input["parts"].extend({"inline_data":{"mime_type":image["mime_type"],"data":image["data"]}} for image in images)
        if audio and last_input:last_input["parts"].extend({"inline_data":{"mime_type":item["mime_type"],"data":item["data"]}} for item in audio)
    else:
        raise HTTPException(422, "不支持的模型接口格式")
    apply_effort(config, body)
    if streaming and fmt == "openai":
        body["stream_options"] = {"include_usage": True}
    if config.get('_tools'):
        from avatar.models.tools import configure_tools
        configure_tools(fmt,body,config['_tools'],required=not config.get('_optional_tools'),
                        batch=bool(config.get('_batch_tools')),native=bool(config.get('_react_tool_calls')))
    return fmt, endpoint, headers, body
