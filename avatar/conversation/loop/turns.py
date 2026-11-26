"""Consume one visible stream; reset uncommitted deltas on interruption."""
import time
from contextlib import aclosing

from fastapi import HTTPException

from avatar.core.execution import check_execution_async
from avatar.models.client import ModelResult


async def stream_final(source, emit, validate, progress=None):
    # Expose the first delta immediately, then validate and emit batches instead of querying SQL per token.
    chunks, pending = [], []
    final = None
    next_emit = 0
    try:
        async with aclosing(source):
            async for event in source:
                if event.get('reset_reply'):
                    chunks.clear()
                    pending.clear()
                    next_emit = 0
                    if progress: progress(event)
                    continue
                if event['type'] == 'delta':
                    pending.append(event['text'])
                    if time.monotonic() < next_emit:
                        continue
                elif event['type'] == 'usage':
                    final = event
                else:
                    continue
                await check_execution_async(validate)
                if pending:
                    text = ''.join(pending)
                    chunks.append(text)
                    emit(text)
                    pending.clear()
                    next_emit = time.monotonic() + .1
        if final is None:
            raise HTTPException(502, '模型流缺少结束回执，未保存回答')
    except HTTPException:
        if chunks and progress:
            progress({'type':'status', 'stage':'thinking', 'reset_reply':True, 'message':'本步回答未完成，正在核对进度'})
        raise
    return ModelResult(''.join(chunks), final['usage'], tool_calls=tuple(final.get('tool_calls', ())),
                       finish_reason=final.get('finish_reason'),
                       partial_tool_calls=tuple(final.get('partial_tool_calls', ())), replay=final.get('replay'))
