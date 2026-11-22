"""Reasoning effort for configured generation APIs and per-owner chat overrides."""
from typing import Literal, get_args

Effort = Literal['low', 'medium', 'high', 'xhigh', 'max']
LEVELS = get_args(Effort)
STANDARD = LEVELS[:3]


def levels(config):
    provider = config.get('provider')
    model = config.get('model', '').lower().removeprefix('models/')
    if not model:
        return ()
    fmt = config.get('api_format', 'openai')
    if fmt == 'gemini':
        if model.startswith('gemini-3-pro'):
            return ('low', 'high')
        if model.startswith(('gemini-4-neon', 'gemini-4-hydrogen')):
            return LEVELS[:4]
        return STANDARD if provider == 'custom' or model.startswith(('gemini-2.5-', 'gemini-3', 'gemini-4')) else ()
    if fmt == 'anthropic':
        if provider != 'custom' and not model.startswith(('claude-opus-4-5','claude-opus-4-6','claude-opus-4-7','claude-opus-4-8','claude-opus-5','claude-sonnet-4-6','claude-sonnet-5','claude-haiku-5','claude-mythos','claude-fable')):
            return ()
        if model.startswith('claude-opus-4-5'):
            return STANDARD
        if model.startswith(('claude-opus-4-6', 'claude-sonnet-4-6')):
            return (*STANDARD, 'max')
        return LEVELS
    if provider == 'deepseek':
        return ('low', 'high', 'max') if model.startswith(('deepseek-v4','deepseek-flash','deepseek-pro')) else ()
    if provider == 'openai':
        if '-pro' in model or 'chat-latest' in model or model.startswith(('o1-mini','o1-preview','gpt-4')):
            return ()
        if model.startswith(('gpt-6', 'gpt-5.5')):
            return LEVELS
        if model.startswith(('gpt-5.2','gpt-5.3','gpt-5.4')):
            return LEVELS[:4]
        return STANDARD if model.startswith(('gpt-5', 'o1', 'o3', 'o4')) else ()
    if provider == 'qwen':
        if model.startswith(('qwen3.8', 'qwen4')):
            return LEVELS[:4]
        return STANDARD if model.startswith(('qwen3.', 'qwen-plus', 'qwen3-')) and 'instruct' not in model else ()
    if provider == 'siliconflow':
        if 'deepseek-v4' in model or 'glm-5.2' in model:
            return ('high', 'max')
        return STANDARD if '/qwen3-' in model and 'instruct' not in model else ()
    if provider == 'openrouter':
        return LEVELS[:4]
    # A custom deployment may use an opaque alias. Its operator selects the supported level.
    return LEVELS if provider == 'custom' else ()


def with_chat_effort(config, override):
    if override and override in levels(config):
        return {**config, 'reasoning_effort': override}
    return config


def visible_output_config(config):
    """Retry a step that exhausted output without any usable characters."""
    provider = config.get('provider')
    model = config.get('model','').split('/')[-1].lower()
    if config.get('api_format') == 'openai' and provider == 'custom' and model.startswith('qwen'):
        return {**config,'reasoning_effort':None,'_recover_thinking':True}
    supported = levels(config)
    if supported and (provider != 'custom' or config.get('reasoning_effort')):
        return {**config,'reasoning_effort':supported[0]}
    return config


def thinking_budget(config, effort):
    divisor, ceiling = {'low': (8, 512), 'medium': (3, 2048), 'high': (2, 8192)}[effort]
    return min(ceiling, max(128, config['max_tokens'] // divisor))


def apply_effort(config, body):
    effort = config.get('reasoning_effort')
    if not effort:
        return
    fmt = config['api_format']
    if fmt == 'gemini':
        if config['model'].removeprefix('models/').startswith('gemini-2.5-'):
            thinking = {'thinkingBudget': thinking_budget(config, effort)}
        else:
            thinking = {'thinkingLevel': effort.upper()}
        body['generationConfig']['thinkingConfig'] = thinking
    elif fmt == 'anthropic':
        body['output_config'] = {'effort': effort}
    elif config['provider'] in ('qwen', 'siliconflow') and levels(config) == STANDARD:
        body.update(enable_thinking=True, thinking_budget=thinking_budget(config, effort))
    elif config['provider'] == 'openrouter':
        body['reasoning'] = {'effort': effort}
    else:
        body['reasoning_effort'] = effort
        if config['provider'] == 'siliconflow':
            body['enable_thinking'] = True
        if config['provider'] == 'deepseek':
            body['thinking'] = {'type': 'enabled'}
