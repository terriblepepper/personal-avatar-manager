"""Private execution checkpoints and validated local artifact receipts."""
import json
from hashlib import sha256

from fastapi import HTTPException

from avatar.core.execution import run_blocking


def resume_safe(state, *, automatic=False, initial=False):
    if (state.get('sandbox') or {}).get('in_flight'):
        return False
    phase = state.get('phase')
    return (phase in ('planning', 'decision', 'writing', 'artifact_receipt')
            or phase is None and initial or phase == 'retry' and not automatic)


def verified_context(receipts, files, *, compact=False):
    """Keep verified IDs and modes accessible even after older observations leave the model view."""
    previous = [{k:r[k] for k in ('goal_no','tool','mode') if r.get(k)} for r in (receipts[-2:] if compact else receipts)]
    live = {item['id']:item for item in files if item.get('id')}
    if not previous and not live:
        return ''
    # An older confirmed dimension must stay visible when newer attachments fill the view.
    selected = live
    if compact:
        modes = {item['html_mode']:item for item in live.values() if item.get('html_mode') in ('2d','3d','html')}
        selected = {item['id']:item for item in (*modes.values(),*list(live.values())[-2:])}
    cards = [{'id':item['id'], 'mode':item.get('html_mode'),
              **({} if compact else {'filename':str(item.get('filename',''))})} for item in selected.values()]
    return '\n已核验的本轮回执（数据）：'+json.dumps(
        {'steps':previous, 'file_count':len(live), 'files':cards}, ensure_ascii=False, separators=(',', ':'))


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
        self.data.update(fields)
        if self.store:
            merge = getattr(self.store, 'merge', None)
            if merge:
                await run_blocking(merge, fields)
            else:
                await run_blocking(self.store.save, self.data)

    async def answer(self, result):
        if not self.store:
            return
        from uuid import uuid4
        name = 'answer-' + uuid4().hex
        data = result.text
        final = {'name': name, 'size': len(data.encode()), 'finish_reason': result.finish_reason,
                 'has_material': result.material_delivery is not None}
        await run_blocking(self.store.append, name, 0, data, patch={
            'final': final, 'usage': result.usage, 'material': list(result.material_delivery or ()), 'phase': 'decision',
        })

    async def file(self, item, args_hash, goal_no, fields):
        name = 'artifact-' + item['id']
        def commit():
            data = item['data']
            receipt = {'id':item['id'], 'filename':item['filename'], 'html_mode':item.get('html_mode','html'),
                       'draft':name, 'size':len(data), 'sha256':sha256(data).hexdigest(),
                       'args_hash':args_hash, 'goal_no':goal_no}
            patch = {**fields, 'files':[*fields['files'], receipt]}
            if self.store:
                size = self.store.append(name, 0, data.decode('utf-8'), patch=patch)
                if size != len(data):
                    raise HTTPException(409, '已生成作品的大小与草稿不一致')
            return patch
        self.data.update(await run_blocking(commit))


async def restore_materials(delivery, entries):
    if not entries:
        return
    if not delivery:
        raise HTTPException(403, '当前会话无权恢复原件')
    for entry in entries:
        fresh = await run_blocking(delivery.library.prepare, entry['id'], expected_path=entry['relative_path'])
        if list(fresh['version']) != list(entry['version']):
            raise HTTPException(409, '资料文件已改变，请重新请求')
        delivery.prepared.append(fresh)
