"""Execute one disclosed entry tool and classify its verified evidence."""
import json

from fastapi import HTTPException

from avatar.core.execution import check_execution_async


async def execute_entry_tool(config, name, args, trace, validate, account, *, is_lookup):
    await check_execution_async(validate)
    tool_input = {'tool':name}
    if name=='read_session_file':
        tool_input['file_access']='directory' if args.get('query') is not None else 'content'
    with trace.step('act',tool_input) as output:
        try:
            note=await config['_execute_entry_tool'](name,args)
        except HTTPException as error:
            if not is_lookup or error.status_code not in (404,409,410,413,415,422) or getattr(error,'delivery_unknown',False):
                raise
            await check_execution_async(validate)
            raise ValueError(str(error.detail)) from None
        if isinstance(note,tuple):
            note,cost=note
            account(cost)
        if not isinstance(note,str) or not note:
            raise ValueError('缺少入口工具回执')
        output['result_chars']=len(note)

    return note


def entry_evidence(name, note):
    if name == 'read_session_file':
        page = json.loads(note)
        if page.get('kind') == 'directory':
            return note, 'directory', True, '目录仅含元数据；需要正文时用有效 file_id 读取。未命中可换查询。'
        if not page.get('has_content'):
            return note, None, False, '只取得文件元数据或空页。请查看后续页或通过已授权的沙箱读取原件后再据内容作答。'
        return note, 'content', True, None
    if name in ('search_materials','read_material'):
        proof = json.loads(note)
        if not isinstance(proof,dict) or proof.get('has_evidence') is not True:
            return note, None, False, '本次检索未获得支持当前目标的材料。请调整查询、申请其他工具，或向用户说明缺口。'
    return note, None, True, None
