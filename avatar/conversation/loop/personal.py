"""Bind a private run to its speaker, source permissions and conversation lease."""
import json
import uuid

from fastapi import HTTPException

from avatar.conversation.jobs.pipeline import validate_authorization
from avatar.conversation.loop.runtime import prepare_checkpoint, resume_safe
from avatar.conversation.loop.store import RunStore
from avatar.knowledge.learning import auto_style_advance
from avatar.skills.policy import require_skills_enabled
from avatar.conversation.pause_state import pending, settle_run_usage, settlement_write
from avatar.knowledge.soul_document import approved_style


STYLE_FIELDS = ('soul', 'soul_version')


def source_snapshot(db, user_id, parent_id, owner, aic):
    row = db.execute("""SELECT a.last_modified_at, a.identity_status, md5(a.profile) AS profile,
        b.id AS soul, md5(b.document) AS soul_version, md5(m.model_config) AS model,
        md5(e.config) AS embedding FROM avatar_records a JOIN users u ON u.id=a.user_id
        LEFT JOIN active_souls s ON s.user_id=a.user_id
        LEFT JOIN soul_builds b ON b.id=s.build_id AND b.user_id=a.user_id AND b.status='approved'
        LEFT JOIN model_settings m ON m.user_id=a.user_id
        LEFT JOIN embedding_settings e ON e.user_id=a.user_id
        WHERE a.user_id=? AND a.aic=? AND u.account_status='active'""",(owner,aic)).fetchone()
    parent = db.execute("""SELECT c.tools_config FROM chat_conversations c JOIN users u ON u.id=c.user_id
        WHERE c.id=? AND c.user_id=? AND u.account_status='active'""",(parent_id,user_id)).fetchone()
    if not row or not parent:
        raise HTTPException(409,'原分身或会话已变更，无法继续此任务')
    files = db.execute("""SELECT id,sha256,md5(description) AS description,filename FROM chat_files
        WHERE user_id=? AND conversation_id=? AND referenced=1 AND origin!='attachment'
        AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP)""",(user_id,parent_id))
    materials = db.execute("""SELECT id,md5(jsonb_build_array(sha256,visibility,filename,relative_path,
        caption,updated_at,extraction_version,animation_digest)::text) AS version
        FROM attachments WHERE user_id=? AND (user_id=? OR visibility='public')""",(owner,user_id))
    folders = db.execute("""SELECT path,md5(jsonb_build_array(caption,visibility)::text) AS version
        FROM material_folders WHERE user_id=? AND (user_id=? OR visibility='public')""",(owner,user_id))
    return {'identity':dict(row),'files':{r['id']:[r['sha256'],r['description'],r['filename']] for r in files},
            'materials':{r['id']:r['version'] for r in materials},'folders':{r['path']:r['version'] for r in folders},
            'skills':{},
            'tools':sorted(k for k,v in json.loads(parent['tools_config']).items() if v is True)}


def check_sources(db, owner, expected, actual):
    if expected.get('skills'):
        require_skills_enabled()
    prior, current = expected['identity'], actual['identity']
    def stable(identity):
        return {key:value for key,value in identity.items() if key not in STYLE_FIELDS}

    if (stable(prior) != stable(current)
            or not set(expected['tools']).issubset(actual['tools'])
            or any(actual[group].get(key) != value for group in ('files','skills','materials','folders')
                   for key,value in expected[group].items())
            or (prior != current and not auto_style_advance(db,owner,prior['soul'],prior['soul_version'],current['soul']))):
        raise HTTPException(409,'原任务资料、文件或工具授权已改变，不能使用原草稿')


def validate_sources_in(db, user_id, parent_id, binding, sources):
    current = source_snapshot(db,user_id,parent_id,binding['owner'],binding['aic'])
    check_sources(db,binding['owner'],sources,current)
    if not db.execute("""SELECT 1 FROM chat_conversations WHERE id=? AND user_id=?
        AND (id=? OR parent_id=?)""",(binding['session_id'],user_id,parent_id,parent_id)).fetchone():
        raise HTTPException(409,'原分身会话已变更，无法继续此任务')


def source_guard(user_id,parent_id,binding,sources):
    stamp=None
    def check(db, *, full=False):
        nonlocal stamp
        # Tuple revisions cover direct updates too. The common path reads no file bodies or JSON payloads.
        row=db.execute("""SELECT a.xmin::text,u.account_status,t.account_status,p.tools_config,p.skills_updated_at,
            m.xmin::text,e.xmin::text,s.xmin::text,b.xmin::text,pinned.xmin::text,ac.updated_at,sc.updated_at,
            (SELECT bit_xor(hashtextextended(id||':'||xmin::text,0)) FROM attachments WHERE user_id=a.user_id),
            (SELECT bit_xor(hashtextextended(path||':'||xmin::text,0)) FROM material_folders WHERE user_id=a.user_id),
            (SELECT bit_xor(hashtextextended(id||':'||xmin::text,0)) FROM agent_skills WHERE user_id=a.user_id),
            (SELECT bit_xor(hashtextextended(skill_id||':'||xmin::text,0)) FROM chat_session_skills WHERE conversation_id=p.id),
            (SELECT bit_xor(hashtextextended(id||':'||xmin::text,0)) FROM chat_files WHERE user_id=p.user_id AND
                conversation_id=p.id AND referenced=1 AND origin!='attachment'
                AND (expires_at IS NULL OR avatar_utc_time(expires_at)>CURRENT_TIMESTAMP))
            FROM chat_conversations p JOIN users u ON u.id=p.user_id
            JOIN chat_conversations child ON child.id=? AND child.user_id=u.id AND (child.id=p.id OR child.parent_id=p.id)
            JOIN avatar_records a ON a.user_id=? AND a.aic=? JOIN users t ON t.id=a.user_id
            LEFT JOIN model_settings m ON m.user_id=a.user_id LEFT JOIN embedding_settings e ON e.user_id=a.user_id
            LEFT JOIN active_souls s ON s.user_id=a.user_id
            LEFT JOIN soul_builds b ON b.id=s.build_id AND b.user_id=a.user_id AND b.status='approved'
            LEFT JOIN soul_builds pinned ON pinned.id=? AND pinned.user_id=a.user_id
            LEFT JOIN attachment_collections ac ON ac.user_id=a.user_id
            LEFT JOIN agent_skill_collections sc ON sc.user_id=a.user_id
            WHERE p.user_id=? AND p.id=?""",(binding['session_id'],binding['owner'],binding['aic'],sources['identity']['soul'],user_id,parent_id)).fetchone()
        if row is None:
            raise HTTPException(409,'原任务会话或身份已改变')
        current=tuple(row)
        if full or current!=stamp:
            validate_sources_in(db,user_id,parent_id,binding,sources)
            stamp=current
    return check


def run_id_for(job, held, request):
    job=dict(job) if job else {}
    return ((json.loads(job['authorization']).get('run_id') if job else None)
            or (held or {}).get('run_id') or ((held or {}).get('snapshot') or {}).get('task_id')
            or (held or {}).get('snapshot',{}).get('agent_pause_id') or (job or {}).get('task_id')
            or request.request_id or uuid.uuid4().hex)


def inspect(chat, user_id, parent_id, run_id, job):
    with chat.service.database.connect() as db:
        validate_authorization(db,job)
        return RunStore.inspect_in(db,user_id,'chat',parent_id,run_id)


def restore(chat, user, request, parent, state):
    binding = state['binding']
    with chat.service.database.connect() as db:
        validate_sources_in(db,user['id'],parent['id'],binding,state['sources'])
        session = db.execute('SELECT * FROM chat_conversations WHERE id=? AND user_id=?',
                             (binding['session_id'],user['id'])).fetchone()
    overview,context = chat.context(user,request,{'user_id':binding['owner'],'aic':binding['aic']},private_access=True,run_parent=parent['id'])
    overview['_parent_id'] = parent['id']
    overview['_target'] = {'aic':binding['aic'],'name':overview['agent']['profile'].get('avatar_name','我的分身')}
    return overview,context,dict(session),None


def attach(chat, user, original, parent, session, overview, context, job, held, run_id, lease, previous):
    owner = overview['agent']
    binding = {'session_id':session['id'],'owner':owner['user_id'],'aic':owner['aic']}
    model = owner.get('model_config') or context[0]
    binding.update(provider=model.get('provider'),model=model.get('model'))
    sources = overview['_run_sources']
    automatic = bool(job and json.loads(job['authorization']).get('resume_run'))
    if previous and not resume_safe(previous, initial=True, automatic=automatic):
        raise HTTPException(409,'上次操作结果未确认，请核对已有结果后结束任务')
    prior = None
    if held and held.get('sources'):
        if any(held[key]!=binding[key] for key in ('owner','aic','session_id')):
            raise HTTPException(409,'原暂停任务的发言人或会话已改变')
        prior = held['sources']
    if previous and previous.get('binding'):
        old = previous['binding']
        if any(old[key]!=binding[key] for key in ('owner','aic','session_id')):
            raise HTTPException(409,'原任务发言人或会话已改变')
        prior = previous['sources']
    if prior:
        with chat.service.database.connect() as db:
            if held and held.get('sources') and held['sources'] != prior:
                check_sources(db,binding['owner'],held['sources'],sources)
            check_sources(db,binding['owner'],prior,sources)
            old_style = prior['identity']['soul']
            if old_style != sources['identity']['soul']:
                prefix, raw = context[2].split('\n',1)
                background = json.loads(raw)
                if 'approved_soul_style' in background:
                    if old_style:
                        background['approved_soul_style'] = approved_style(db,binding['owner'],build_id=old_style)
                    else:
                        background.pop('approved_soul_style')
                    context = (context[0],context[1],prefix+'\n'+json.dumps(background,ensure_ascii=False),context[3])
                sources = {**sources,'identity':prior['identity']}
    check=source_guard(user['id'],parent['id'],binding,sources)
    def validate(db):
        validate_authorization(db,job)
        if not db.execute('SELECT 1 FROM chat_conversations WHERE id=? AND user_id=? AND lease_until=?',
                          (parent['id'],user['id'],lease)).fetchone():
            raise HTTPException(409,'原任务已停止')
        if not (job and job.get('_source_guard')):
            check(db)
    store = RunStore(chat.service.database,user['id'],'chat',parent['id'],run_id,uuid.uuid4().hex,validate)
    state = store.acquire(expected=previous)
    state = prepare_checkpoint(store, state, fields={'binding':binding,'sources':sources}, automatic=automatic)
    config = dict(context[0])
    config.update(_run_store=store,_run_input_id=(job or {}).get('task_id') or original.request_id or original.agent_pause_id or run_id,
                  _run_supplement=original.message.strip() if original.agent_control=='continue' else '',
                  _resume_sandbox=bool(state.get('sandbox')))
    overview['_run_store'] = store
    overview['_source_guard'] = check
    if job:
        job['_source_guard'] = check
    return (config,*context[1:])


def discard_unpaused(store, lease):
    with settlement_write(store.database, store.owner, store.scope_id, store.run_id) as db:
        row=db.execute('SELECT lease_until FROM chat_conversations WHERE id=? AND user_id=?',
                       (store.scope_id,store.owner)).fetchone()
        task=pending(db,store.owner,store.scope_id)
        if row and row['lease_until']==lease and (task or {}).get('run_id')!=store.run_id:
            settle_run_usage(db,store.owner,store.scope_id,store.run_id)
            store.clear_in(db,store.owner,'chat',store.scope_id,store.run_id)
