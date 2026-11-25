"""Report agent phases and tool receipts through the model trace scope."""
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager

from avatar.core.execution import model_failure
from avatar.models.trace import emit_tool_result


_SECRET = re.compile('(?:Bearer\\s+\\S+|github_pat_|sk-[a-z0-9_-]{12,}|-----BEGIN [A-Z ]+KEY-----|(?:api[_ -]?key|password|token|secret)["\\\']?\\s*[:=]\\s*\\S+)', re.I)


def _trace_value(value, depth=0):
    if isinstance(value, str):
        if _SECRET.search(value):
            return '[敏感内容已隐藏]'
        return value[:500] + ('…' if len(value) > 500 else '')
    if depth >= 5:
        return '…'
    if isinstance(value, Mapping):
        return {str(key): _trace_value(item, depth+1) for key, item in list(value.items())[:10]}
    if isinstance(value, (list, tuple)):
        return [_trace_value(item, depth+1) for item in value[:8]]
    return value


class AgentSteps:
    """Publish bounded workflow state. Callers project permitted fields before tracing."""
    PHASES = {'perceive': '感知', 'reason': '推理', 'act': '行动',
              'observe': '观察', 'check': '完成检查', 'final': '最终输出'}

    def __init__(self, progress, *, model_key=''):
        self.progress = progress
        self._model_key = model_key

    def point(self, phase, inputs, output, *, status='completed'):
        if self.progress:
            self.progress({'type': 'tool', 'id': uuid.uuid4().hex, 'group': 'agent_trace',
                           'name': self.PHASES[phase], 'phase': phase, 'status': status,
                           'input': _trace_value(inputs), 'output': _trace_value(output)})

    def tool_result(self, name, arguments, result):
        emit_tool_result(name, arguments, result, secret=self._model_key)

    def receipt(self, text):
        if self.progress:
            data = '[敏感内容已隐藏]' if _SECRET.search(text) else text
            self.progress({'type': 'tool', 'id': uuid.uuid4().hex, 'group': 'agent_receipt',
                           'name': '工具回执', 'status': 'completed', 'output': data})

    @contextmanager
    def step(self, phase, inputs):
        output = {}
        if not self.progress:
            yield output
            return
        event = {'type': 'tool', 'id': uuid.uuid4().hex, 'group': 'agent_trace',
                 'name': self.PHASES[phase], 'phase': phase, 'status': 'running', 'input': _trace_value(inputs)}
        self.progress(event)
        status = 'interrupted'
        try:
            yield output
            status = 'error' if output.get('retry_required') else 'completed'
        except Exception as error:
            status = 'error'
            output['error'] = type(error).__name__
            if hasattr(error, 'status_code'):
                output['status_code'] = error.status_code
                if phase=='reason' and 'iteration' in inputs:
                    output.update(model_failure(error))
            raise
        finally:
            self.progress({**event, 'status': status, 'output': _trace_value(output)})
