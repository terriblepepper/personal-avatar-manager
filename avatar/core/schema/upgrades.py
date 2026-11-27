"""Idempotent upgrades for installations predating the current domain schema."""
from avatar.core.schema.performance import first_migration
from avatar.core.dispatch_schema import install_dispatch
from avatar.core.migrations.peer_states import migrate_peer_states
from avatar.core.migrations.soul import restore_asset_sources
from avatar.core.migrations.task_workspaces import install_task_workspaces


def upgrade(connection):
    connection.execute("""ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_stage
        TEXT NOT NULL DEFAULT 'done' CHECK (onboarding_stage IN ('welcome','tour','done'))""")

    # 解除旧教师注册审核的账户限制，资料及历史审核记录留在原表。
    connection.execute("""UPDATE users SET account_status='active', account_reason=''
        WHERE person_type='teacher' AND account_status IN ('pending','rejected')""")

    connection.execute("""CREATE TABLE IF NOT EXISTS town_access_limits (
        owner_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        visitor_daily_limit BIGINT CHECK(visitor_daily_limit BETWEEN 0 AND 9007199254740991),
        total_daily_limit BIGINT CHECK(total_daily_limit BETWEEN 0 AND 9007199254740991))""")

    connection.execute("ALTER TABLE agent_dialogues ADD COLUMN IF NOT EXISTS agent_checkpoint TEXT, ADD COLUMN IF NOT EXISTS pending_file_delivery TEXT, ADD COLUMN IF NOT EXISTS message_prepared INTEGER NOT NULL DEFAULT 0 CHECK(message_prepared IN (0,1))")
    connection.execute("ALTER TABLE agent_task_files ADD COLUMN IF NOT EXISTS source_file_id TEXT")
    connection.execute("ALTER TABLE profile_records ADD COLUMN IF NOT EXISTS reuse_signed_acs INTEGER")
    connection.execute("ALTER TABLE profile_submissions ADD COLUMN IF NOT EXISTS reuse_signed_acs INTEGER")

    migrate_names = connection.execute("""INSERT INTO data_migrations(name,applied_at)
        VALUES('unified_avatar_name',now()::text) ON CONFLICT DO NOTHING RETURNING name""").fetchone()
    if migrate_names:
        # 校正平台资料，已签发协议文档和历史审核快照保持原文。
        connection.execute("""WITH names AS (
            SELECT user_id, coalesce(CASE WHEN acs_document::jsonb #>> '{entityMeta,ontologyAic}' IS NOT NULL
                THEN nullif(acs_document::jsonb #>> '{entityMeta,avatarName}','') END,
                nullif(acs_document::jsonb->>'name',''),nullif(acs_input::jsonb->>'name','')) AS name
            FROM avatar_records)
            UPDATE avatar_records a SET profile=jsonb_set(a.profile::jsonb,'{avatar_name}',to_jsonb(n.name))::text
            FROM names n WHERE a.user_id=n.user_id AND n.name IS NOT NULL
            AND a.profile::jsonb->>'avatar_name' IS DISTINCT FROM n.name""")
        connection.execute("""WITH names AS (
            SELECT p.user_id, coalesce(CASE WHEN p.status='active' OR p.reuse_signed_acs=1
                    THEN a.profile::jsonb->>'avatar_name' END,
                nullif(p.acs_document::jsonb->>'name',''), nullif(p.acs_input::jsonb->>'name',''),
                a.profile::jsonb->>'avatar_name') AS name
            FROM profile_records p LEFT JOIN avatar_records a USING(user_id))
            UPDATE profile_records p SET draft=jsonb_set(p.draft::jsonb,'{avatar_name}',to_jsonb(n.name))::text,
                last_modified_at=CASE WHEN p.status IN ('active','draft','rejected') THEN
                    to_char(greatest(now(),nullif(p.last_modified_at,'')::timestamptz + interval '1 microsecond')
                        AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00' ELSE p.last_modified_at END,
                acs_input=CASE WHEN p.acs_input::jsonb ? 'name' THEN
                    jsonb_set(p.acs_input::jsonb,'{name}',to_jsonb(n.name))::text ELSE p.acs_input END
            FROM names n WHERE p.user_id=n.user_id AND n.name IS NOT NULL AND
            (p.draft::jsonb->>'avatar_name' IS DISTINCT FROM n.name OR
             (p.acs_input::jsonb ? 'name' AND p.acs_input::jsonb->>'name' IS DISTINCT FROM n.name))""")

    for table in ('attachments', 'chat_files'):
        connection.execute(f"""ALTER TABLE {table}
            ADD COLUMN IF NOT EXISTS animation_metadata TEXT NOT NULL DEFAULT '{{}}',
            ADD COLUMN IF NOT EXISTS animation_digest TEXT NOT NULL DEFAULT ''""")
    connection.execute("""CREATE INDEX IF NOT EXISTS attachment_animation_digest ON attachments(user_id,animation_digest)
        WHERE animation_digest<>''""")
    connection.execute("""ALTER TABLE chat_files ADD COLUMN IF NOT EXISTS source_animation_id
        TEXT REFERENCES attachments(id) ON DELETE CASCADE""")
    connection.execute("""CREATE TABLE IF NOT EXISTS chat_message_materials (
        message_id BIGINT NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
        attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
        PRIMARY KEY(message_id,attachment_id))""")
    connection.execute("ALTER TABLE chat_message_materials ADD COLUMN IF NOT EXISTS position INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE INDEX IF NOT EXISTS chat_message_material_attachment ON chat_message_materials(attachment_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS attachments_owner_hash ON attachments(user_id,sha256)")

    asset_source = connection.execute("""SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='knowledge_assets' AND column_name='source_attachment_id'""")
    if asset_source.fetchone() is None:
        connection.execute("ALTER TABLE knowledge_assets ADD COLUMN source_attachment_id TEXT NOT NULL DEFAULT ''")
        restore_asset_sources(connection)

    connection.execute("""CREATE TABLE IF NOT EXISTS chat_agent_pauses (
        conversation_id TEXT PRIMARY KEY REFERENCES chat_conversations(id) ON DELETE CASCADE,
        pause_id TEXT NOT NULL, document TEXT NOT NULL)""")
    state_check = connection.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid='chat_generation_jobs'::regclass AND conname='chat_generation_jobs_state_check'""").fetchone()
    if state_check and 'awaiting_input' not in state_check[0]:
        connection.execute("""ALTER TABLE chat_generation_jobs DROP CONSTRAINT chat_generation_jobs_state_check,
            ADD CONSTRAINT chat_generation_jobs_state_check CHECK
            (state IN ('queued','running','succeeded','failed','cancelled','indeterminate','awaiting_input'))""")

    install_task_workspaces(connection)
    migrate_peer_states(connection)
    if first_migration(connection, 'personal-task-message-ids-v1'):
        connection.execute("""UPDATE avatar_aip_tasks SET document=(document::jsonb ||
            jsonb_strip_nulls(jsonb_build_object(
                'id', CASE WHEN coalesce(document::jsonb->>'id','')='' THEN replace(gen_random_uuid()::text,'-','') END,
                'sentAt', CASE WHEN coalesce(document::jsonb->>'sentAt','')='' THEN
                    to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END)))::text
            WHERE coalesce(document::jsonb->>'id','')='' OR coalesce(document::jsonb->>'sentAt','')=''""")
    if first_migration(connection, 'generated-chat-files-expiry-v1'):
        connection.execute("""UPDATE chat_files SET expires_at=to_char(
            (created_at::timestamptz AT TIME ZONE 'UTC') + interval '7 days','YYYY-MM-DD HH24:MI:SS')
            WHERE origin='generated' AND expires_at IS NULL
            AND NOT EXISTS(SELECT 1 FROM agent_shared_workspaces w WHERE w.conversation_id=chat_files.conversation_id)
            AND NOT EXISTS (
                SELECT 1 FROM agent_task_members m WHERE m.resource_conversation_id=chat_files.conversation_id
                AND m.user_id=chat_files.user_id)""")


    install_dispatch(connection)
    connection.execute('''CREATE TABLE IF NOT EXISTS ops_metric_minutes (
        minute BIGINT PRIMARY KEY, metrics JSONB NOT NULL)''')
