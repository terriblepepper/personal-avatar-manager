"""Check response and terminal action against verified outputs and grants."""
from avatar.conversation.tools.material_delivery import valid_reply


TERMINAL_TOOLS = frozenset(('send_message', 'end_task', 'wait_for_user'))


class FinishGate:
    def __init__(self, config, policy, delivery, drafts):
        self.config, self.policy, self.delivery, self.drafts = config, policy, delivery, drafts

    @property
    def html_missing(self):
        return self.policy.require_html and not any(
            item.get('html_mode') in ('html','2d','3d')
            for item in (*self.config.get('_generated_files', ()), *self.config.get('_agent_saved_files', ()))
        )

    def check(self, text, action=None):
        if action and action.get('arguments', {}).get('stop_requested') is True:
            if not self.config.get('_peer_can_stop'):
                return '只有本人最新补充明确要求结束时可设置 stop_requested。'
        elif self.html_missing and (not action or action['name']!='wait_for_user'):
            return '用户已开启「制作动画」。请调用 create_html 生成实际作品后交付。'
        if not action and self.policy.require_terminal_tool:
            return '请通过 send_message、end_task 或 wait_for_user 提交本轮决定。'
        if self.drafts.active and (not action or action['name'] in ('send_message','end_task')):
            if not action or not action['arguments'].get('stop_requested'):
                return '仍有未完成的 HTML 草稿。请按最后确认的字节游标续写并保存后再回复。'
        files = {item['id'] for group in ('_generated_files','_agent_saved_files','_react_public_files')
                 for item in self.config.get(group, ())}
        if self.delivery:
            files.update(item['id'] for item in self.delivery.prepared)
        if not valid_reply(text, files):
            return '正文有无效的附件标记或内部地址。请使用真实回执的 ID，作品会随本轮答复展示。'
        if not text.strip() and not action:
            return '请根据本轮已取得的结果给出可见答复。'
        return None
