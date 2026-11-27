"""Upgrade stable business identities, cache accounting and PostgreSQL access paths."""
from hashlib import sha256

from psycopg import sql


def first_migration(connection, name):
    return connection.execute("""INSERT INTO data_migrations(name,applied_at)
        VALUES(%s, CURRENT_TIMESTAMP::text) ON CONFLICT DO NOTHING RETURNING name""", (name,)).fetchone()


def install_business_keys(connection):
    # Renaming preserves identity sequences, checkpoint FKs and existing file URLs.
    names = {'avatar_aip_tasks':'task_number', 'agent_wire_results':'result_number',
             'chat_generation_jobs':'queue_number'}
    obsolete = ('rag_lexical_records', 'model_artifact_cache', 'agent_wire_receipts')
    rows = connection.execute("""SELECT table_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND column_name='_rowid' AND table_name=ANY(%s)""",
        (list(names) + list(obsolete),)).fetchall()
    for (table,) in rows:
        action = (sql.SQL('RENAME COLUMN _rowid TO {}').format(sql.Identifier(names[table]))
                  if table in names else sql.SQL('DROP COLUMN _rowid'))
        connection.execute(sql.SQL('ALTER TABLE {} {}').format(sql.Identifier(table), action))


# Existing TEXT timestamps include both UTC SQLite dates and ISO offsets.
# An immutable projection accepts absolute ISO dates only, fixing the session's timezone and date style.
UTC_TIME = """CREATE OR REPLACE FUNCTION avatar_utc_time(value text) RETURNS timestamptz
    LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE SET TIME ZONE 'UTC' SET DateStyle='ISO,YMD' AS $$
    BEGIN
        IF value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)?)?$' THEN
            RETURN NULL;
        END IF;
        RETURN value::timestamptz;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow OR invalid_time_zone_displacement_value
        THEN RETURN NULL;
    END $$"""

INDEXES = (
    "chat_conversation_list ON chat_conversations(user_id,platform,channel,caller_id,updated_at DESC)",
    "chat_message_page ON chat_messages(conversation_id,id)",
    "chat_generation_latest ON chat_generation_jobs(conversation_id,user_id,created_at DESC,queue_number DESC)",
    "peer_task_member_owner ON agent_task_members(user_id,hidden)",
    "peer_session_member_owner ON agent_session_members(user_id,hidden)",
    "peer_dialogue_claim ON agent_dialogues(updated_at,created_at,id) WHERE status IN ('queued','awaiting_identity') AND (accepted=1 OR delivery_mode='message')",
    "peer_dialogue_running ON agent_dialogues(session_id) WHERE status='running'",
    "peer_identity_retry ON agent_dialogues(retry_until) WHERE status='awaiting_identity'",
    "peer_controls_due ON agent_protocol_controls(next_attempt) WHERE status='queued'",
    "rag_claim_due ON file_rag_documents(next_attempt_at,user_id,updated_at,file_id) WHERE status='queued'",
    "rag_claim_oldest ON file_rag_documents(updated_at,file_id) WHERE status='queued'",
    "attachments_visible ON attachments(user_id,visibility,created_at DESC)",
    "profile_review_pending ON profile_submissions(submitted_at,id) WHERE status='pending'",
    "login_limit_expiry ON login_limits(reset_at)",
    "recovery_limit_expiry ON recovery_limits(reset_at)",
    "session_expiry ON sessions(expires)",
    "oidc_state_expiry ON oidc_login_states(expires)",
    "entry_token_expiry ON platform_entry_tokens(expires)",
    "cache_owner_recent ON model_artifact_cache(user_id,updated_at DESC,kind,signature,digest) INCLUDE(byte_size)",
    "chat_file_expiry_time ON chat_files(avatar_utc_time(expires_at)) WHERE expires_at IS NOT NULL",
    "diagram_expiry_time ON conversation_diagrams(avatar_utc_time(expires_at)) WHERE expires_at IS NOT NULL",
    "ended_dialogue_expiry ON agent_dialogues(avatar_utc_time(expires_at)) WHERE status='ended'",
    "pending_intent_time ON agent_intents(avatar_utc_time(created_at)) WHERE message_id IS NULL AND status='pending'",
    "town_visitor_expiry ON town_visitor_requests(created_at)",
    "aip_active_deadline ON avatar_aip_tasks(deadline) WHERE document::jsonb->'status'->>'state'='working'",
    "aip_guest_sent ON avatar_aip_tasks((document::jsonb->>'sentAt')) WHERE strpos(peer_aic,'#guest:')>0",
    "workspace_member_lookup ON agent_task_members(resource_conversation_id,user_id)",
    "wire_result_sequence ON agent_wire_results(task_id,session_id,local_aic,peer_aic,result_number DESC)",
)

# Scope catalog-driven additions to tables owned by this application's migration.
FK_TABLES = """
account_reviews active_souls agent_dialogue_controls agent_dialogue_messages agent_dialogues agent_intents
agent_loop_runs agent_peer_auto_accept agent_peer_sessions agent_pending_inputs agent_request_receipts
agent_session_members agent_task_files agent_task_members agent_task_memories audit_events avatar_aip_task_commands
avatar_identity_changes avatar_identity_jobs chat_conversations chat_files chat_generation_jobs chat_messages
chat_pending chat_session_skills conversation_diagrams identity_migration_previews model_calls platform_bindings
platform_entry_tokens profile_submissions rag_publications sessions soul_builds style_learning_runs style_learning_settings
""".split()

MISSING_FK_INDEXES = """SELECT t.relname, array(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum,n)
 JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum ORDER BY n)
FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
WHERE c.contype='f' AND t.relnamespace=current_schema()::regnamespace AND t.relname=ANY(%s) AND NOT EXISTS
 (SELECT 1 FROM pg_index i WHERE i.indrelid=t.oid AND i.indisready AND i.indisvalid AND i.indpred IS NULL
 AND i.indnkeyatts>=cardinality(c.conkey)
 AND ARRAY(SELECT key FROM unnest(c.conkey) AS key ORDER BY key)
     = ARRAY(SELECT key FROM unnest(i.indkey) WITH ORDINALITY k(key,n)
          WHERE n<=cardinality(c.conkey) ORDER BY key))
ORDER BY 1,2"""


def install_indexes(connection):
    if not first_migration(connection, 'postgres-access-paths-v1'):
        return
    connection.execute(UTC_TIME)
    connection.execute("""ALTER TABLE model_artifact_cache ADD COLUMN IF NOT EXISTS byte_size bigint
        GENERATED ALWAYS AS (octet_length(value)::bigint) STORED""")
    for index in INDEXES:
        connection.execute('CREATE INDEX IF NOT EXISTS ' + index)
    # Each superseded non-unique index has an equivalent full FK index, timestamp projection or prefix.
    connection.execute("""DROP INDEX IF EXISTS avatar_aip_working_deadline, avatar_aip_guest_sent_at,
        attachments_owner, chat_files_expiry, agent_dialogues_expiry, completed_task_workspace_expiry,
        agent_task_members_workspace, chat_source_animation, agent_loop_job, agent_loop_chat,
        agent_loop_dialogue, agent_loop_protocol""")
    for table, columns in connection.execute(MISSING_FK_INDEXES, (FK_TABLES,)).fetchall():
        name = 'fk_' + table + '_' + '_'.join(columns)
        if len(name) > 63:
            name = 'fk_' + table + '_' + sha256(name.encode()).hexdigest()[:10]
        connection.execute(sql.SQL('CREATE INDEX {} ON {} ({})').format(
            sql.Identifier(name), sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier,columns))))
