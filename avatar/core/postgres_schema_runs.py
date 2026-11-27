"""Private generation checkpoints installed after the business scope tables."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS agent_loop_runs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        owner TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        scope TEXT NOT NULL CHECK(scope IN ('chat','dialogue','protocol')), scope_id TEXT NOT NULL,
        run_id TEXT NOT NULL, writer TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 1,
        retired_writers TEXT[] NOT NULL DEFAULT '{}',
        state JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(state)='object' AND octet_length(state::text)<=4194304),
        storage_bytes BIGINT NOT NULL DEFAULT 0 CHECK(storage_bytes>=0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        chat_job TEXT REFERENCES chat_generation_jobs(task_id) ON DELETE CASCADE,
        chat_id TEXT GENERATED ALWAYS AS (CASE WHEN scope='chat' THEN scope_id END) STORED
            REFERENCES chat_conversations(id) ON DELETE CASCADE,
        dialogue_id TEXT GENERATED ALWAYS AS (CASE WHEN scope='dialogue' THEN scope_id END) STORED
            REFERENCES agent_dialogues(id) ON DELETE CASCADE,
        protocol_id BIGINT GENERATED ALWAYS AS (CASE WHEN scope='protocol' THEN scope_id::bigint END) STORED
            REFERENCES avatar_aip_tasks(task_number) ON DELETE CASCADE,
        UNIQUE(owner,scope,scope_id,run_id))""",
    """CREATE INDEX IF NOT EXISTS agent_loop_expiry ON agent_loop_runs(updated_at)""",
    """CREATE TABLE IF NOT EXISTS agent_loop_chunks (
        run UUID NOT NULL REFERENCES agent_loop_runs(id) ON DELETE CASCADE,
        draft TEXT NOT NULL, start_byte BIGINT NOT NULL CHECK(start_byte>=0),
        is_binary BOOLEAN NOT NULL, content BYTEA NOT NULL,
        PRIMARY KEY(run,draft,start_byte))""",
)
