"""Versioned checkpoints and validated local artifact receipts."""
import json
import re
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from avatar.core.execution import AskUser, run_blocking
from avatar.core.text_budget import model_text_budget
from avatar.models.client import ModelResult
from avatar.models.context_budget import bounded_excerpt


_LEGACY_SAFE = {'planning', 'decision', 'writing', 'artifact_receipt'}
_LEGACY_BINDINGS = (
    'binding', 'sources', 'files', 'sandbox', 'peer_source', 'peer_style', 'peer_workspace',
    'objective', 'usage', 'session_source_checks',
)


def _valid_held_question(question):
    if not isinstance(question, dict):
        return False
    payload = dict(question)
    identifier = payload.pop('id', None)
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
        return False
    try:
        # Existing questions were checked against the model budget that produced them.
        with model_text_budget(len(json.dumps(payload)) // 3 + 1):
            AskUser.model_validate(payload)
    except ValidationError:
        return False
    return True


def resume_safe(state, *, automatic=False, initial=False, allow_question=True):
    if (state.get('sandbox') or {}).get('in_flight'):
        return False
    phase = state.get('phase')
    if state.get('schema_version') == 2:
        if phase == 'paused' and state.get('user_question') is not None:
            return allow_question and _valid_held_question(state['user_question'])
        return phase in ('running', 'completed') or not automatic and phase in ('paused', 'retry')
    return phase in _LEGACY_SAFE or phase is None and initial or phase in ('paused', 'retry') and not automatic


def _legacy_summary(state):
    summary = state.get('history_summary') or {}
    sections = [summary.get('text', ''), (summary.get('turn') or {}).get('text', ''),
                (summary.get('objective') or {}).get('text', '')]
    sections.extend(note for note in state.get('observations', ()) if isinstance(note, str))
    sections.extend(supplement for supplement in state.get('supplements', ()) if isinstance(supplement, str))
    sections.extend(f'{item["question"]}：{item["answer"]}' for item in state.get('question_answers', ())
                    if isinstance(item, dict) and isinstance(item.get('question'), str)
                    and isinstance(item.get('answer'), str))
    return bounded_excerpt('\n'.join(filter(None, sections)), 1200)


async def load_checkpoint(store, *, automatic=False, allow_question=True):
    state = await run_blocking(store.load) if store is not None else {}
    if state.get('schema_version') == 2 or not resume_safe(
            state, initial=True, automatic=automatic, allow_question=allow_question):
        return state
    fresh = {key: state[key] for key in _LEGACY_BINDINGS if key in state}
    fresh.update(schema_version=2, phase='retry' if state.get('phase') == 'retry' else 'running',
                 step=0, trajectory=[], drafts={}, receipt_refs={}, supplements=[], question_answers=[])
    if material := (state.get('entry_material') or {}).get('revisions'):
        fresh['entry_material'] = {'revisions':material}
    historic = state.get('session_file_reads', state.get('generated_reads', {}))
    if historic:
        fresh['session_source_checks'] = {**fresh.get('session_source_checks', {}), **historic}
    if summary := _legacy_summary(state):
        fresh['legacy_summary'] = summary
    if store is not None:
        keep = {receipt['draft'] for receipt in fresh.get('files', ())}
        keep.update('sb-' + receipt['token'][:32] for receipt in (fresh.get('sandbox') or {}).get('receipts', ()))
        await run_blocking(store.migrate, fresh, keep)
    return fresh


async def restore_files(store, state, config):
    """The caller has already checked the run owner, scope, source versions and speaker."""
    existing = {item['id'] for item in (*config.get('_generated_files', ()), *config.get('_agent_saved_files', ()))}
    persisted = set(config.get('_agent_persisted_file_ids', ()))
    for receipt in state.get('files', ()):
        identifier = receipt['id']
        if identifier in existing or identifier in persisted:
            continue

        def restore():
            data = store.read(receipt['draft']).encode('utf-8')
            if len(data) != receipt['size'] or sha256(data).hexdigest() != receipt['sha256']:
                raise HTTPException(409, '原作品的校验值发生变化，无法恢复')
            return {key: receipt[key] for key in ('id', 'filename', 'html_mode')} | {'data': data}

        config.setdefault('_generated_files', []).append(await run_blocking(restore))
        existing.add(identifier)


class LoopState:
    def __init__(self, store, state):
        self.store = store
        self.data = state

    async def save(self, **fields):
        if self.store is not None:
            await run_blocking(self.store.merge, fields)
        self.data.update(fields)

    async def answer(self, result, **fields):
        name = 'answer-' + uuid4().hex
        encoded = result.text.encode('utf-8')
        final = {'name': name, 'size': len(encoded), 'sha256': sha256(encoded).hexdigest(),
                 'finish_reason': result.finish_reason, 'has_material': result.material_delivery is not None,
                 'tool_calls': list(result.tool_calls), 'material_sources': list(result.material_sources),
                 'contact_draft': result.contact_draft}
        patch = {**fields, 'final': final, 'usage': result.usage,
                 'material': list(result.material_delivery or ()), 'phase': 'completed'}
        if self.store is not None:
            if encoded:
                await run_blocking(self.store.append, name, 0, result.text, patch=patch)
            else:
                await run_blocking(self.store.merge, patch)
        self.data.update(patch)

    async def restore_answer(self, delivery):
        if self.store is None or self.data.get('phase') != 'completed':
            return None
        final = self.data['final']
        text = await run_blocking(self.store.read, final['name']) if final['size'] else ''
        encoded = text.encode('utf-8')
        if len(encoded) != final['size'] or sha256(encoded).hexdigest() != final['sha256']:
            raise HTTPException(409, '已完成正文的校验值发生变化，无法恢复')
        return ModelResult(text, self.data.get('usage'), tuple(final['tool_calls']), final['finish_reason'],
                           material_sources=tuple(final['material_sources']), contact_draft=final['contact_draft'],
                           material_delivery=(tuple(delivery.prepared if delivery else ()) if final['has_material'] else None))



async def restore_materials(delivery, entries):
    if not entries:
        return
    if not delivery:
        raise HTTPException(403, '当前会话无权恢复原件')
    restored = []
    for entry in entries:
        if entry['id'] not in delivery.visible_ids:
            raise HTTPException(409, '原件回执缺少本轮可见凭证，请重新查找')
        fresh = await run_blocking(delivery.library.prepare, entry['id'], expected_path=entry['relative_path'])
        if list(fresh['version']) != list(entry['version']):
            raise HTTPException(409, '资料文件已改变，请重新请求')
        restored.append(fresh)
    delivery.prepared.extend(restored)
