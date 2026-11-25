"""Validate the final text against actual artifact and delivery receipts."""
import re

from fastapi import HTTPException

from avatar.conversation.tools.activity import without_false_tool_footer
from avatar.conversation.tools.material_delivery import valid_reply


def completion_reply(complete, config, delivery, receipts):
    if '[[attachment:' in complete or re.search(r'(?:\]\(\s*<?|<)(?:/api/|sandbox:|blob:|data:|file:)', complete, re.I):
        available_ids = {i['id'] for i in [*receipts, *(delivery.prepared if delivery else ())]}
        if not valid_reply(complete,available_ids):
            return None
    if config.get('_sandbox_succeeded'):
        complete = without_false_tool_footer(complete)
    receipt = None
    if delivery:
        if delivery.failure:
            complete = '本轮未附上资料原件。'+delivery.failure
            receipt = ()
        elif delivery.prepared:
            if not valid_reply(complete,{item['id'] for item in [*delivery.prepared, *config.get('_generated_files', [])]}):
                raise HTTPException(502,'附件正文引用无效，未发送原件')
            receipt = tuple(delivery.prepared)
    return complete, receipt
