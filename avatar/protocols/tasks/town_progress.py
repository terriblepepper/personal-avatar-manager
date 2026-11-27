"""Live status and idle deadlines for accepted personal AIP tasks."""
import logging
import time
import uuid

from avatar.core.db import now
from avatar.protocols.tasks import personal_turns as turns


WORKING_TIMEOUT = 480
PROGRESS_INTERVAL = 8
log = logging.getLogger('uvicorn.error.' + __name__)
PUBLIC_STAGES = {
    'memory': '读取会话上下文', 'compressing': '整理会话上下文',
    'intent_analysis': '判断任务目标', 'thinking': '分析任务和已有信息',
    'tool_selection': '判断所需工具', 'rag_retrieving': '检索相关资料',
    'material_reading': '读取相关资料', 'tool_catalog': '查询候选资料',
    'tool_action': '执行已选择的操作',
    'result_writing': '整理最终答复', 'model_stream': '模型响应流已连接',
}


class WorkingProgress:
    def __init__(self, result, run):
        self.result_id = result['id']
        self.ids = result['taskId'], result['sessionId'], run['run_id']
        self.run = run
        self.streaming = False
        self.started = time.monotonic()
        self.message = '任务已接收，等待可用执行资源'
        self.revision = 0
        self.snapshot_key = None
        self.envelope = {}

    def update(self, event):
        if event.get('group') == 'model_io':
            status = event['status']
            if status not in ('completed','error','interrupted'):
                return
            attempt = event['input']['attempt']
            output = event['output']
            log.info('Personal AIP model taskId=%r sessionId=%r run_id=%s purpose=%s attempt=%d status=%s interrupted=%s elapsed_ms=%d',
                     *self.ids, event['purpose'], attempt, status, output.get('stream_interrupted', False),
                     round(1000*(time.monotonic()-self.started)))
            stage = (f"模型响应流中断，正在进行第 {output['retry']}/{output['retry_limit']} 次重试"
                     if output.get('retry') else None)
        elif event.get('type') == 'status':
            stage = PUBLIC_STAGES.get(event.get('stage'))
        else:
            return
        if stage and stage != self.message:
            self.message = stage
            self.revision += 1

    def stream_state(self, active):
        now_at = time.time()
        if active == self.streaming or (active and self.run['work_until'] <= now_at):
            return
        self.streaming = active
        # A recovered stream starts with a full waiting budget after it closes again.
        self.run['work_until'] = now_at+WORKING_TIMEOUT
        if active:
            self.update({'type':'status','stage':'model_stream'})

    def deadline(self):
        now_at = time.time()
        if self.streaming:
            return now_at+WORKING_TIMEOUT
        until = self.run['work_until']
        if until <= now_at:
            raise turns.WorkDeadlineExceeded
        return until

    def view(self, result):
        # Progress is a new message, while the ledger retains the working ID used by finish().
        elapsed = max(0, int(time.monotonic()-self.started))
        key = (self.revision, elapsed//PROGRESS_INTERVAL)
        if self.snapshot_key != key:
            self.snapshot_key = key
            self.envelope = {'id':uuid.uuid4().hex, 'sentAt':now(),
                             'status':{**result['status'], 'dataItems':[
                                 {'type':'text','text':f'{self.message}（已等待 {elapsed} 秒）'}]}}
        return {**result, **self.envelope}
