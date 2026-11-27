"""Commit signals for existing PostgreSQL ledgers."""
from psycopg import sql


# The channel carries only opaque routing keys. Query APIs still check ownership.
TRIGGERS = (
    ('chat', 'chat_generation_jobs', 'INSERT OR UPDATE OF state', """
        IF TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM NEW.state THEN
            PERFORM avatar_emit('work', 'chat');
        END IF;
        PERFORM avatar_emit('generation', NEW.user_id, NEW.request_id);
    """),
    ('peer', 'agent_dialogues', 'INSERT OR UPDATE', """
        IF TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status THEN
            PERFORM avatar_emit('work', 'peer');
        END IF;
        PERFORM avatar_emit('dialogue', NEW.user_id);
        PERFORM avatar_emit('dialogue', NEW.target_user_id);
    """),
    ('peer_members', 'agent_session_members', 'INSERT OR UPDATE OR DELETE', """
        PERFORM avatar_emit('dialogue', CASE WHEN TG_OP='DELETE' THEN OLD.user_id ELSE NEW.user_id END);
    """),
    ('task_members', 'agent_task_members', 'INSERT OR UPDATE OR DELETE', """
        PERFORM avatar_emit('dialogue', CASE WHEN TG_OP='DELETE' THEN OLD.user_id ELSE NEW.user_id END);
    """),
    ('controls', 'agent_protocol_controls', 'INSERT OR UPDATE OF status,next_attempt', """
        IF NEW.status='queued' THEN PERFORM avatar_emit('work', 'controls'); END IF;
    """),
    ('index', 'file_rag_documents', 'INSERT OR UPDATE OF status,next_attempt_at,generation', """
        IF NEW.status='queued' THEN
            PERFORM avatar_emit('work', 'index');
        END IF;
    """),
    ('soul', 'soul_builds', 'INSERT OR UPDATE OF status OR DELETE', """
        IF TG_OP <> 'DELETE' AND NEW.status='queued' THEN PERFORM avatar_emit('work', 'soul'); END IF;
        IF (TG_OP <> 'DELETE' AND NEW.status='pending') OR (TG_OP <> 'INSERT' AND OLD.status='pending') THEN
            PERFORM avatar_emit('reviews');
        END IF;
    """),
    ('provision', 'provision_jobs', 'INSERT OR UPDATE OF status,next_attempt_at,registration_phase', """
        IF NEW.status IN ('queued','waiting') THEN PERFORM avatar_emit('work', 'provision'); END IF;
        PERFORM avatar_emit('overview', (SELECT user_id FROM profile_submissions WHERE id=NEW.submission_id));
    """),
    ('overview_profile', 'profile_records', 'INSERT OR UPDATE OF status,reason,draft', """
        PERFORM avatar_emit('overview', NEW.user_id);
    """),
    ('overview_avatar', 'avatar_records', 'INSERT OR UPDATE OF aic,profile,identity_status OR DELETE', """
        PERFORM avatar_emit('overview', CASE WHEN TG_OP='DELETE' THEN OLD.user_id ELSE NEW.user_id END);
    """),
    ('topology_avatar', 'avatar_records', 'INSERT OR UPDATE OF aic,identity_status OR DELETE', """
        IF TG_OP != 'UPDATE' OR OLD.aic IS DISTINCT FROM NEW.aic
                OR OLD.identity_status IS DISTINCT FROM NEW.identity_status THEN
            PERFORM avatar_emit('work', 'topology');
        END IF;
    """),
    ('topology_account', 'users', 'UPDATE OF account_status', """
        IF OLD.account_status IS DISTINCT FROM NEW.account_status THEN
            PERFORM avatar_emit('work', 'topology');
        END IF;
    """),
    ('topology_port', 'avatar_entity_ports', 'INSERT OR UPDATE OR DELETE', """
        PERFORM avatar_emit('work', 'topology');
    """),
    ('topology_oidc', 'oidc_identities', 'INSERT OR UPDATE OR DELETE', """
        PERFORM avatar_emit('work', 'topology');
    """),
    ('topology_link', 'oidc_identity_links', 'INSERT OR UPDATE OR DELETE', """
        PERFORM avatar_emit('work', 'topology');
    """),
    ('identity', 'avatar_identity_jobs', 'INSERT OR UPDATE OF status,next_attempt_at', """
        IF NEW.status IN ('queued','waiting') THEN
            PERFORM avatar_emit('work', 'identity');
        ELSE
            PERFORM avatar_emit('work', 'provision');
        END IF;
    """),
    ('inbox', 'agent_notifications', 'INSERT OR UPDATE OF read_at,deleted_at OR DELETE', """
        IF TG_OP = 'INSERT' AND NEW.kind='activity' THEN RETURN NULL; END IF;
        PERFORM avatar_emit('inbox', CASE WHEN TG_OP='DELETE' THEN OLD.user_id ELSE NEW.user_id END);
    """),
    ('audit', 'audit_events', 'INSERT', """
        PERFORM avatar_emit('inbox', NEW.user_id);
        IF NEW.actor_id IS DISTINCT FROM NEW.user_id THEN PERFORM avatar_emit('inbox', NEW.actor_id); END IF;
    """),
    ('reviews', 'profile_submissions', 'INSERT OR UPDATE OF status OR DELETE', """
        IF (TG_OP <> 'DELETE' AND NEW.status='pending') OR (TG_OP <> 'INSERT' AND OLD.status='pending') THEN
            PERFORM avatar_emit('reviews');
        END IF;
    """),
)


def install_dispatch(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS avatar_dispatch_events (
        id BIGSERIAL PRIMARY KEY, payload TEXT NOT NULL)''')
    connection.execute("""
        CREATE OR REPLACE FUNCTION avatar_emit(VARIADIC keys text[]) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            mode text := current_setting('avatar.dispatch_enabled', true);
        BEGIN
            IF mode = 'detect' THEN
                -- Probe once per external write transaction. If no relay owns the schema,
                -- hold a shared lock so its first scan follows this transaction's commit.
                mode := CASE WHEN pg_try_advisory_xact_lock_shared(
                    hashtextextended('avatar.dispatch:' || current_schema(), 0))
                    THEN 'off' ELSE 'on' END;
                PERFORM set_config('avatar.dispatch_enabled', mode, true);
            END IF;
            IF mode = 'on' THEN
                INSERT INTO avatar_dispatch_events(payload) VALUES (array_to_json(keys)::text);
                PERFORM pg_notify('avatar_dispatch_' || md5(current_schema()::text), '');
            END IF;
        END
        $$
    """)
    for key, table, operation, body in TRIGGERS:
        name = sql.Identifier(f'avatar_dispatch_{key}')
        connection.execute(sql.SQL("""
            CREATE OR REPLACE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                {}
                RETURN NULL;
            END
            $$
        """).format(name, sql.SQL(body)))
        connection.execute(sql.SQL("""
            CREATE OR REPLACE TRIGGER {} AFTER {} ON {}
            FOR EACH ROW EXECUTE FUNCTION {}()
        """).format(name, sql.SQL(operation), sql.Identifier(table), name))
