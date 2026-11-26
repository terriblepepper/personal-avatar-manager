"""Discover and stage public originals inside the current ReAct turn."""
from avatar.conversation.tools.material_delivery import FindMaterial
from avatar.conversation.tools.material_search import MaterialDiscovery
from avatar.core.execution import run_blocking
from avatar.files.library.public import PublicMaterialRequest, PublicMaterials, SEND_PUBLIC_MATERIAL


FIND_PUBLIC_MATERIAL = {
    'name': 'find_public_material',
    'description': '查找本人公开资料的文件、目录和正文。用返回的 file_id 选择要交付的原件。',
    'parameters': FindMaterial.model_json_schema(),
}


def bind_public_material_tools(chat, config, library, owner, store, state, *,
                               workspace=None, task_id=None, allow_sources=False):
    if state.get('schema_version') != 2:
        state = {}
    visible = set(state.get('public_visible', ()))
    exports = list(state.get('public_files', ()))
    discovery = None

    def allowed():
        if allow_sources:
            return visible | {item['file_id'] for item in config.get('_material_sources', ()) if item.get('file_id')}
        return visible

    def documents():
        return library.documents(public_only=True)

    async def find(args):
        nonlocal discovery
        request = FindMaterial.model_validate(args)
        if discovery is None:
            discovery = MaterialDiscovery(await run_blocking(documents), chat.rag.retrieve, refresh=documents)
        result = await discovery.page(request)
        found = {item['file_id'] for item in result['items']} - visible
        if found:
            await run_blocking(store.merge, {'public_visible': sorted(visible | found)})
            visible.update(found)
        return result

    public = PublicMaterials(chat)

    async def send(args):
        request = PublicMaterialRequest.model_validate(args)
        requested = set(request.file_ids)
        if not requested.issubset(allowed()):
            raise ValueError('请先查找公开资料，再选择可见的原件 ID')
        known = {item['public_material_id']: item for item in exports}
        if len(known.keys() | requested) > 5:
            raise ValueError('本轮最多交付五份公开资料原件')
        originals = await run_blocking(public.prepare, owner, request, workspace, task_id,
                                       reserved={item['id'] for item in exports})
        fresh = []
        for item in originals:
            if prior := known.get(item['public_material_id']):
                if any(prior[key] != item[key] for key in ('filename', 'sha256', 'size')):
                    raise ValueError('已准备的资料原件有变化，请重新发起请求')
            else:
                fresh.append(item)
                known[item['public_material_id']] = item
        if fresh:
            await run_blocking(store.merge, {'public_files': [*exports, *fresh]})
            exports.extend(fresh)
        files = [known[identifier] for identifier in dict.fromkeys(request.file_ids)]
        return {'files': [{'id': item['id'], 'filename': item['filename'],
                           'marker': '[[attachment:' + item['id'] + ']]'} for item in files]}

    config['_public_material_ready'] = lambda: bool(allowed())
    config['_react_public_files'] = exports
    config.setdefault('_entry_tools', []).extend((FIND_PUBLIC_MATERIAL, SEND_PUBLIC_MATERIAL))
    config.setdefault('_react_handlers', {}).update(find_public_material=find, send_public_material=send)
    return allowed
