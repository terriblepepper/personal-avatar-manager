"""分身发言的持久边界。业务状态、资料授权与私有草稿在同一数据库事务内核对。"""
import hashlib
import json
import uuid

from fastapi import HTTPException

from avatar.core.db import dump, now
from avatar.conversation.loop.runtime import prepare_checkpoint, resume_safe
from avatar.conversation.loop.store import RunStore
from avatar.knowledge.learning import auto_style_advance


def active_peer_style(db, owner):
    style=db.execute("""SELECT b.id,md5(b.document) AS checksum FROM active_souls s
        JOIN soul_builds b ON b.id=s.build_id AND b.user_id=s.user_id AND b.status='approved'
        WHERE s.user_id=?""",(owner,)).fetchone()
    return dict(style) if style else {'id':None,'checksum':None}


def check_peer_style(db, owner, pinned):
    current=active_peer_style(db,owner)
    if current!=pinned and not auto_style_advance(db,owner,pinned['id'],pinned['checksum'],current['id']):
        raise HTTPException(409,'原任务绑定的 Soul 风格已撤回或修改')


def speaker(task):
    return task['target_user_id'] if task['next_speaker'] else task['user_id']


def wire_id(db, task, owner):
    count = db.execute('SELECT COUNT(*) FROM agent_dialogue_messages WHERE dialogue_id=?', (task['id'],)).fetchone()[0]
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ':'.join([task['id'], str(task['revision']), str(count), owner])))


def pause_wire(db, task, checkpoint):
    owner = speaker(task)
    command = wire_id(db, task, owner)
    key = (task['id'], task['session_id'], command,
           task['target_aic'] if task['next_speaker'] else task['own_aic'],
           task['own_aic'] if task['next_speaker'] else task['target_aic'])
    row = db.execute('SELECT state,document FROM agent_wire_results WHERE task_id=? AND session_id=? AND command_id=? '
                     'AND local_aic=? AND peer_aic=?', key).fetchone()
    if row is None:
        checkpoint['unconfirmed_wire'] = command
        return
    if row['state'] != 'working':
        return
    result = json.loads(row['document'])
    stamp = now()
    result.update(id=uuid.uuid4().hex,sentAt=stamp,status={'state':'awaiting-input','stateChangedAt':stamp,
                  'dataItems':[{'type':'text','text':'任务已暂停'},{'type':'data','data':{'revision':task['revision']}}]})
    db.execute("UPDATE agent_wire_results SET state='awaiting-input',document=? WHERE task_id=? AND session_id=? AND command_id=? AND local_aic=? AND peer_aic=? AND state='working'",
               (dump(result), *key))


class PeerTurn:
    def __init__(self, service, task, owner):
        self.service, self.task, self.owner = service, task, owner
        self.writer = uuid.uuid4().hex
        self.active = None
        self.billing = None
        self.validate_session_files = None
        with service.database.connect(write=True) as db:
            self.workspace = service.resources.workspace(db, owner, task['session_id'], task['id'])
            self.settings_workspace = service.resources.settings_workspace(db, owner, task['session_id'], task['id'])
            state=RunStore.inspect_in(db,owner,'dialogue',task['id'],'turn') or {}
            receipt=json.loads(task.get('pending_file_delivery') or '{}')
            prior=state or (receipt if receipt.get('speaker')==owner else {})
            if prior and 'peer_style' not in prior:
                raise HTTPException(409, '原任务缺少已确认的分身风格凭证')
            self.style = prior['peer_style'] if prior else active_peer_style(db,owner)
            self.source = self.authority(db)

    def authority(self, db):
        task = self.service.sessions.owned(db, {'id': self.owner}, self.task['id'])
        if (task['status'] != 'running' or task['revision'] != self.task['revision']
                or speaker(task) != self.owner or not (task['accepted'] or task['delivery_mode'] == 'message')):
            raise HTTPException(409, '分身任务状态或当前发言方已变化')
        if key := self.task.get('_wire_key'):
            if not db.execute("SELECT 1 FROM agent_wire_results WHERE local_aic=? AND peer_aic=? AND command_id=? "
                              "AND task_id=? AND session_id=? AND state='working'", (*key,task['id'],task['session_id'])).fetchone():
                raise HTTPException(409, '分身协议命令已停止')
        return self.source_version(self.service, db, task, self.owner, self.workspace, style=self.style)

    @staticmethod
    def source_version(service, db, task, owner, workspace, *, style=None):
        side = 'initiator' if owner == task['user_id'] else 'target'
        table,key=('agent_session_members','session_id') if task['delivery_mode']=='message' else ('agent_task_members','task_id')
        scope_id=task['session_id'] if key=='session_id' else task['id']
        settings = db.execute(f'SELECT resource_conversation_id FROM {table} WHERE {key}=? AND user_id=? AND hidden=0',
                              (scope_id,owner)).fetchone()
        row = db.execute('SELECT c.tools_config,m.cleared_through,md5(model.model_config),md5(embedding.config) FROM chat_conversations c JOIN agent_session_members m '
                         'ON m.user_id=c.user_id AND m.session_id=? LEFT JOIN model_settings model ON model.user_id=c.user_id '
                         'LEFT JOIN embedding_settings embedding ON embedding.user_id=c.user_id WHERE c.id=? AND c.user_id=?',
                         (task['session_id'], settings[0] if settings else workspace, owner)).fetchone()
        if row is None:
            raise HTTPException(409, '分身会话资料授权已失效')
        own, target = service.sessions.participants(db, task['user_id'], task['target_aic'])
        if own['aic'] != task['own_aic'] or target['user_id'] != task['target_user_id']:
            raise HTTPException(409, '参与分身的身份已变化')
        return hashlib.sha256(dump([service.output_revision(db,task,owner,style=style), task[side+'_skills'],
                                    task[side+'_excluded_skills'], *row]).encode()).hexdigest()

    def validate(self, db):
        if self.authority(db) != self.source:
            raise HTTPException(409, '分身资料或工具授权已改变，原草稿已停止使用')

    def commit(self, db, *, files=True):
        if self.active is None:
            self.validate(db)
        else:
            self.active.check_in(db)
        if files and self.validate_session_files:
            self.validate_session_files(db,lock=True)

    def check(self):
        with self.service.database.connect() as db:
            self.commit(db,files=False)

    @staticmethod
    def _legacy_staged_public(state):
        # The old export receipt was written after local preparation, before publishing the peer reply.
        export = state.get('peer_export')
        return (state.get('schema_version') != 2 and state.get('phase') == 'external_handoff'
                and (state.get('handoff') or {}).get('name') == 'send_public_material'
                and isinstance(export, dict) and isinstance(export.get('exports'), list)
                and bool(export.get('receipts')) and not (state.get('sandbox') or {}).get('in_flight'))

    @staticmethod
    def resume_safe(state, *, automatic=False, initial=False):
        return PeerTurn._legacy_staged_public(state) or resume_safe(state, automatic=automatic, initial=initial)

    def open(self):
        store = RunStore(self.service.database, self.owner, 'dialogue', self.task['id'], 'turn', self.writer, self.validate)
        state = store.acquire()
        if state and state.get('peer_source') != self.source:
            raise HTTPException(409, '分身资料或工具授权已改变，原草稿已停止使用')
        if not self.resume_safe(state, initial=not state):
            raise HTTPException(409, '原操作结果尚未确认，请核对后结束任务')
        self.active = store
        fields = {'peer_source':self.source, 'peer_style':self.style, 'peer_workspace':self.workspace}
        if not state:
            fields['objective'] = self.task['objective']
        if self._legacy_staged_public(state):
            state = {**state, 'phase':'decision'}
        return store, prepare_checkpoint(store, state, fields=fields)

    def charge(self, config, usage):
        if usage is not None:
            self.billing = (config['provider'], config['model'], usage)

    def settle(self, db):
        if self.billing is None:
            return None
        provider, model, usage = self.billing
        db.execute('INSERT INTO model_calls(user_id,provider,model,usage,created_at,purpose,caller_id) VALUES(?,?,?,?,?,?,?)',
                   (self.owner,provider,model,dump(usage),now(),'agent_dialogue',self.task['user_id']))
        self.active.mark_usage_recorded_in(db)
        return usage

    @staticmethod
    def clear(db, task_id, owner):
        RunStore.clear_scope_in(db, owner, 'dialogue', task_id)

    @staticmethod
    def recoverable(service, db, task):
        owner = speaker(task)
        receipt = json.loads(task['pending_file_delivery'] or '{}')
        known = (receipt.get('speaker') == owner and receipt.get('revision') == task['revision']
                 and bool(receipt.get('answer')))
        state = RunStore.inspect_in(db, owner, 'dialogue', task['id'], 'turn') or {}
        if state or known:
            snapshot = state or receipt
            if 'peer_style' not in snapshot:
                return False
            workspace = state.get('peer_workspace') or receipt.get('workspace')
            try:
                current = PeerTurn.source_version(service, db, task, owner, workspace, style=snapshot['peer_style'])
            except HTTPException:
                return False
            if (state and state.get('peer_source') != current) or (known and receipt.get('authority') != current):
                return False
        safe = PeerTurn.resume_safe(state, automatic=True)
        if task['transport_mode'] == 'acps':
            command_id = wire_id(db, task, owner)
            wire = db.execute('SELECT state FROM agent_wire_results WHERE task_id=? AND session_id=? AND command_id=? '
                              'AND local_aic=? AND peer_aic=?',
                              (task['id'], task['session_id'], command_id,
                               task['target_aic'] if task['next_speaker'] else task['own_aic'],
                               task['own_aic'] if task['next_speaker'] else task['target_aic'])).fetchone()
            return bool(wire and (wire['state'] in ('awaiting-completion','awaiting-input')
                                  or wire['state'] == 'working' and (known or safe)))
        return known or safe