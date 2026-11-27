"""Batch the full paired-session list while preserving the detail API projection."""
import json
from collections import defaultdict

from avatar.conversation.collaboration.transport import current_protocol


def full_page(service, db, user, ids):
    if not ids:
        return []
    owner = user['id']
    pairs = db.execute("""SELECT s.id,s.accepted,m.cleared_through,m.summary_through,m.resource_conversation_id AS settings,
            w.conversation_id AS workspace,a.user_id AS peer,a.aic,a.profile,own.aic AS own_aic,
            nullif(e.config::jsonb->>'encrypted_key','') IS NOT NULL AS embedded,
            nullif(oe.config::jsonb->>'encrypted_key','') IS NOT NULL AS own_embedded,
            auto.user_id IS NOT NULL AS auto_accept,EXISTS(SELECT 1 FROM agent_dialogues WHERE session_id=s.id) AS has_task
        FROM agent_peer_sessions s JOIN agent_session_members m ON m.session_id=s.id AND m.user_id=? AND m.hidden=0
        JOIN avatar_records a ON a.user_id=CASE WHEN s.left_user_id=m.user_id THEN s.right_user_id ELSE s.left_user_id END
        LEFT JOIN avatar_records own ON own.user_id=m.user_id
        LEFT JOIN embedding_settings e ON e.user_id=a.user_id LEFT JOIN embedding_settings oe ON oe.user_id=m.user_id
        LEFT JOIN agent_shared_workspaces w ON w.scope='session' AND w.scope_id=s.id
        LEFT JOIN agent_peer_auto_accept auto ON auto.user_id=m.user_id AND auto.peer_user_id=a.user_id
        WHERE s.id=ANY(?::text[]) ORDER BY array_position(?::text[],s.id)""",(owner,ids,ids)).fetchall()
    messages = db.execute("""SELECT tail.* FROM agent_session_members scope CROSS JOIN LATERAL (
        SELECT m.*,d.objective AS task_title,d.user_id AS initiator_id,d.target_user_id AS recipient_id,
            d.own_aic AS initiator_aic,d.target_aic AS recipient_aic FROM agent_dialogue_messages m
        JOIN agent_dialogues d ON d.id=m.dialogue_id WHERE d.session_id=scope.session_id AND d.delivery_mode='message'
          AND m.id>scope.cleared_through AND NOT EXISTS(
            SELECT 1 FROM agent_task_members tm WHERE tm.task_id=d.id AND tm.user_id=scope.user_id AND tm.hidden=1)
        ORDER BY m.id DESC LIMIT 10) tail
        WHERE scope.user_id=? AND scope.hidden=0 AND scope.session_id=ANY(?::text[]) ORDER BY tail.id""",(owner,ids)).fetchall()
    message_task_ids = {r['dialogue_id'] for r in messages}
    tasks = db.execute("""WITH visible AS (
        SELECT d.*, (d.status<>'ended' OR m.cleared_at='' OR
            avatar_utc_time(coalesce(d.ended_at,d.updated_at))>avatar_utc_time(nullif(m.cleared_at,''))) AS fresh,
            m.cleared_through FROM agent_dialogues d JOIN agent_session_members m ON m.session_id=d.session_id
        WHERE m.user_id=? AND m.hidden=0 AND d.session_id=ANY(?::text[]) AND NOT EXISTS(
            SELECT 1 FROM agent_task_members v WHERE v.task_id=d.id AND v.user_id=m.user_id AND v.hidden=1)),
        listed AS (
            SELECT *,row_number() OVER (PARTITION BY session_id ORDER BY created_at DESC,id DESC) AS rank,
                count(*) FILTER (WHERE delivery_mode<>'message') OVER (PARTITION BY session_id) AS total
            FROM visible v WHERE fresh OR EXISTS(SELECT 1 FROM agent_dialogue_messages WHERE dialogue_id=v.id AND id>v.cleared_through)),
        chat AS (SELECT DISTINCT ON (session_id) session_id,id FROM visible WHERE delivery_mode='message' AND fresh
            ORDER BY session_id,updated_at DESC,id DESC)
        SELECT v.*, l.rank,l.total,(c.id IS NOT NULL) AS latest_chat FROM visible v
        LEFT JOIN listed l ON l.id=v.id LEFT JOIN chat c ON c.id=v.id
        WHERE l.rank<=10 OR c.id IS NOT NULL OR v.id=ANY(?::text[]) ORDER BY l.rank,v.id""",(owner,ids,list(message_task_ids))).fetchall()
    views = {item['id']:item for item in service.views(db,tasks,user)}
    lists, tails, message_tasks = defaultdict(list), defaultdict(list), defaultdict(dict)
    chats, totals = {}, {}
    for row in tasks:
        sid = row['session_id']
        if row['rank'] is not None:
            totals[sid] = row['total']
            if row['rank'] <= 10:
                lists[sid].append(views[row['id']])
        if row['latest_chat']:
            chats[sid] = views[row['id']]
    for message in service.project_messages(db,messages,user,set()):
        task = views[message['dialogue_id']]
        sid = task['session_id']
        tails[sid].append(message)
        message_tasks[sid][task['id']] = task
    result = []
    acps = getattr(service.transport(),'acps',None)
    for pair in pairs:
        sid = pair['id']
        protocol = current_protocol(pair['own_aic'],pair['aic'],ready=bool(acps and acps.ready(owner,pair['peer']))) if pair['own_aic'] else ''
        result.append({
            'session_id':sid,'id':sid,'viewer_user_id':owner,'accepted':bool(pair['accepted']),'can_accept':False,
            'auto_accept':pair['auto_accept'],'auto_accept_available':pair['has_task'],'current_transport_mode':protocol,
            'embedding_status':{'configured':pair['own_embedded'],'peerConfigured':pair['embedded']},
            'resource_conversation_id':pair['workspace'],'settings_conversation_id':pair['settings'],
            'peer_aic':pair['aic'],'peer_name':json.loads(pair['profile']).get('avatar_name','分身'),
            'messages':tails[sid],'has_more':len(tails[sid])==10,'chat':chats.get(sid),
            'message_tasks':list(message_tasks[sid].values()),'tasks':lists[sid],'task_total':totals.get(sid,0),
            'summary_through':pair['summary_through'] if pair['summary_through']>pair['cleared_through'] else 0,
        })
    return result
