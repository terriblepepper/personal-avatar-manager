"""Independent task workspaces share the task's completion-based deadline."""

def install_task_workspaces(connection):
    connection.execute("ALTER TABLE agent_task_members ADD COLUMN IF NOT EXISTS expired_files TEXT NOT NULL DEFAULT '[]'")
    connection.execute("""CREATE INDEX IF NOT EXISTS agent_task_members_workspace
        ON agent_task_members(resource_conversation_id,user_id) WHERE resource_conversation_id IS NOT NULL""")
    connection.execute("""CREATE INDEX IF NOT EXISTS completed_task_workspace_expiry
        ON agent_dialogues(expires_at) WHERE delivery_mode<>'message' AND status='ended' AND expires_at IS NOT NULL""")
    connection.execute("""CREATE OR REPLACE FUNCTION avatar_task_workspace_expiry() RETURNS trigger
        LANGUAGE plpgsql AS $body$
        DECLARE supplied_deadline text := NEW.expires_at;
        BEGIN
            SELECT d.expires_at INTO NEW.expires_at
            FROM agent_task_members m JOIN agent_dialogues d ON d.id=m.task_id
            JOIN chat_conversations c ON c.id=m.resource_conversation_id AND c.user_id=m.user_id AND c.channel='peer'
            WHERE m.resource_conversation_id=NEW.conversation_id AND m.user_id=NEW.user_id
                AND d.delivery_mode<>'message' AND m.hidden=0
                AND NOT EXISTS(SELECT 1 FROM agent_session_members s WHERE s.resource_conversation_id=c.id)
            ORDER BY d.expires_at DESC NULLS FIRST LIMIT 1;
            IF NOT FOUND THEN
                NEW.expires_at := supplied_deadline;
                IF TG_OP='INSERT' AND EXISTS(
                    SELECT 1 FROM chat_conversations WHERE id=NEW.conversation_id AND channel='peer'
                ) THEN
                    NEW.expires_at := to_char((CURRENT_TIMESTAMP AT TIME ZONE 'UTC') + interval '7 days',
                                              'YYYY-MM-DD HH24:MI:SS');
                END IF;
            END IF;
            RETURN NEW;
        END $body$""")
    for table, trigger in (('chat_files','peer_file_expiry'),('conversation_diagrams','peer_diagram_expiry')):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        connection.execute(f"""CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE OF expires_at ON {table}
            FOR EACH ROW EXECUTE FUNCTION avatar_task_workspace_expiry()""")
    repair = connection.execute("""INSERT INTO data_migrations(name,applied_at)
        VALUES('task_workspace_deadlines',now()::text) ON CONFLICT DO NOTHING RETURNING name""").fetchone()
    if repair:
        connection.execute("""WITH shared_empty AS (
            SELECT m.resource_conversation_id FROM agent_task_members m
            WHERE m.resource_conversation_id IS NOT NULL
              AND NOT EXISTS(SELECT 1 FROM chat_files f WHERE f.conversation_id=m.resource_conversation_id)
              AND NOT EXISTS(SELECT 1 FROM conversation_diagrams f WHERE f.conversation_id=m.resource_conversation_id)
            GROUP BY m.resource_conversation_id HAVING count(*)>1)
            UPDATE agent_task_members m SET resource_conversation_id=NULL
            FROM shared_empty s, agent_dialogues d
            WHERE m.resource_conversation_id=s.resource_conversation_id AND d.id=m.task_id AND d.delivery_mode<>'message'""")
        connection.execute("""UPDATE agent_dialogues d SET expires_at=to_char(
            (coalesce(nullif(d.ended_at,''),d.updated_at)::timestamptz AT TIME ZONE 'UTC') + interval '7 days',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            FROM agent_protocol_controls control
            WHERE d.id=control.task_id AND control.action='complete'
              AND d.delivery_mode<>'message' AND d.status='ended' AND d.expires_at IS NULL""")
        for table in ('chat_files','conversation_diagrams'):
            connection.execute(f"""UPDATE {table} f SET expires_at=d.expires_at
                FROM agent_task_members m JOIN agent_dialogues d ON d.id=m.task_id
                JOIN chat_conversations c ON c.id=m.resource_conversation_id AND c.user_id=m.user_id AND c.channel='peer'
                WHERE m.resource_conversation_id=f.conversation_id AND m.user_id=f.user_id
                    AND d.delivery_mode<>'message' AND m.hidden=0 AND f.expires_at IS DISTINCT FROM d.expires_at
                    AND NOT EXISTS(SELECT 1 FROM agent_session_members s WHERE s.resource_conversation_id=c.id)""")
