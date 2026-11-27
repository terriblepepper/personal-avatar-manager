"""Collaboration tables, indexes and triggers."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS agent_activity_cursors (
      user_id TEXT PRIMARY KEY REFERENCES users(id), last_event BIGINT NOT NULL DEFAULT 0,
      admin BIGINT NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS agent_dialogue_preferences (
      user_id TEXT PRIMARY KEY REFERENCES users(id), max_rounds BIGINT NOT NULL DEFAULT 10 CHECK(max_rounds BETWEEN 1 AND 1000))""",
    """CREATE TABLE IF NOT EXISTS agent_notifications (
      id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
      session_id TEXT, task_id TEXT, title TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
      read_at TEXT, deleted_at TEXT, dedupe_key TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'chat', target_page TEXT NOT NULL DEFAULT '', admin_only BIGINT NOT NULL DEFAULT 0, UNIQUE(user_id,dedupe_key))""",
    """CREATE TABLE IF NOT EXISTS agent_peer_sessions (
      id TEXT PRIMARY KEY, left_user_id TEXT NOT NULL REFERENCES users(id),
      right_user_id TEXT NOT NULL REFERENCES users(id), creator_id TEXT NOT NULL REFERENCES users(id),
      accepted BIGINT NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(left_user_id,right_user_id), CHECK(left_user_id<right_user_id))""",
    """CREATE TABLE IF NOT EXISTS agent_dialogues (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), own_aic TEXT NOT NULL,
      target_user_id TEXT NOT NULL REFERENCES users(id), target_aic TEXT NOT NULL, objective TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued', active_speaker TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '', initiator_confirmed BIGINT NOT NULL DEFAULT 0,
      target_confirmed BIGINT NOT NULL DEFAULT 0, request_id TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '', summary_through BIGINT NOT NULL DEFAULT 0, next_speaker BIGINT NOT NULL DEFAULT 0, initiator_deleted BIGINT NOT NULL DEFAULT 0, target_deleted BIGINT NOT NULL DEFAULT 0, initiator_done BIGINT NOT NULL DEFAULT 0, target_done BIGINT NOT NULL DEFAULT 0, ended_at TEXT, expires_at TEXT, end_reason TEXT NOT NULL DEFAULT '', initiator_skills TEXT NOT NULL DEFAULT '', target_skills TEXT NOT NULL DEFAULT '', source_conversation_id TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL, retry_until DOUBLE PRECISION NOT NULL DEFAULT 0, session_id TEXT REFERENCES agent_peer_sessions(id), round_limit BIGINT NOT NULL DEFAULT 10, rounds_done BIGINT NOT NULL DEFAULT 0, task_kind TEXT NOT NULL DEFAULT 'chat', shared_content TEXT NOT NULL DEFAULT '', initiator_excluded_skills TEXT NOT NULL DEFAULT '[]', target_excluded_skills TEXT NOT NULL DEFAULT '[]', delivery_mode TEXT NOT NULL DEFAULT 'negotiation', transport_mode TEXT NOT NULL DEFAULT '', waiting_reason TEXT NOT NULL DEFAULT '', tool_activity TEXT NOT NULL DEFAULT '[]', accepted BIGINT NOT NULL DEFAULT 0, confirmation_required BIGINT NOT NULL DEFAULT 0, confirmation_acknowledged BIGINT NOT NULL DEFAULT 0, UNIQUE(user_id,request_id))""",
    """CREATE TABLE IF NOT EXISTS agent_session_members (
      session_id TEXT NOT NULL REFERENCES agent_peer_sessions(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), hidden BIGINT NOT NULL DEFAULT 0,
      cleared_through BIGINT NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
      summary_through BIGINT NOT NULL DEFAULT 0, source_conversation_id TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL, cleared_at TEXT NOT NULL DEFAULT '', resource_conversation_id TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL,
      PRIMARY KEY(session_id,user_id))""",
    """CREATE TABLE IF NOT EXISTS agent_dialogue_controls (
      dialogue_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), request_id TEXT NOT NULL,
      PRIMARY KEY(dialogue_id,user_id,request_id))""",
    """CREATE TABLE IF NOT EXISTS agent_dialogue_messages (
      id BIGSERIAL PRIMARY KEY, dialogue_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      turn BIGINT NOT NULL, speaker_aic TEXT NOT NULL, speaker_name TEXT NOT NULL, content TEXT NOT NULL,
      usage TEXT NOT NULL, created_at TEXT NOT NULL, speaker_role TEXT NOT NULL DEFAULT 'agent', transport_mode TEXT NOT NULL DEFAULT '', task_action TEXT NOT NULL DEFAULT '', speaker_user_id TEXT REFERENCES users(id), state TEXT NOT NULL DEFAULT '', state_detail TEXT NOT NULL DEFAULT '', tool_activity TEXT NOT NULL DEFAULT '[]', UNIQUE(dialogue_id,turn))""",
    """CREATE TABLE IF NOT EXISTS agent_intents (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
      document TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', task_id TEXT, message_id BIGINT REFERENCES chat_messages(id) ON DELETE SET NULL)""",
    """CREATE TABLE IF NOT EXISTS agent_request_receipts (
      user_id TEXT NOT NULL REFERENCES users(id), scope TEXT NOT NULL, request_id TEXT NOT NULL,
      fingerprint TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      PRIMARY KEY(user_id,scope,request_id))""",
    """CREATE TABLE IF NOT EXISTS agent_pending_inputs (
      id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), request_id TEXT NOT NULL, message TEXT NOT NULL,
      applied BIGINT NOT NULL DEFAULT 0, skill_commands TEXT NOT NULL DEFAULT '', removed_skills TEXT NOT NULL DEFAULT '[]', UNIQUE(task_id,user_id,request_id))""",
    """CREATE TABLE IF NOT EXISTS agent_protocol_controls (
      task_id TEXT PRIMARY KEY REFERENCES agent_dialogues(id) ON DELETE CASCADE, action TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued', next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS agent_task_members (
      task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      user_id TEXT NOT NULL REFERENCES users(id), hidden BIGINT NOT NULL DEFAULT 0,
      summary TEXT NOT NULL DEFAULT '', summary_through BIGINT NOT NULL DEFAULT 0, resource_conversation_id TEXT REFERENCES chat_conversations(id) ON DELETE SET NULL,
      PRIMARY KEY(task_id,user_id))""",
    """CREATE TABLE IF NOT EXISTS agent_task_memories (
      user_id TEXT NOT NULL REFERENCES users(id), session_id TEXT NOT NULL REFERENCES agent_peer_sessions(id) ON DELETE CASCADE,
      task_id TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, source_message_id BIGINT REFERENCES chat_messages(id) ON DELETE SET NULL, PRIMARY KEY(user_id,task_id))""",
    """CREATE TABLE IF NOT EXISTS agent_task_files (
      task_id TEXT NOT NULL REFERENCES agent_dialogues(id) ON DELETE CASCADE,
      file_id TEXT NOT NULL REFERENCES chat_files(id) ON DELETE CASCADE,
      source_user_id TEXT NOT NULL REFERENCES users(id), message_id BIGINT REFERENCES agent_dialogue_messages(id) ON DELETE SET NULL,
      PRIMARY KEY(task_id,file_id))""",
    """CREATE INDEX IF NOT EXISTS agent_dialogues_participants ON agent_dialogues(user_id,target_user_id,created_at)""",
    """CREATE INDEX IF NOT EXISTS agent_dialogues_incoming ON agent_dialogues(target_user_id,created_at)""",
    """CREATE INDEX IF NOT EXISTS agent_dialogues_queue ON agent_dialogues(status,created_at,id)""",
    """CREATE INDEX IF NOT EXISTS agent_notifications_inbox ON agent_notifications(user_id,deleted_at,read_at,id)""",
    """CREATE INDEX IF NOT EXISTS agent_dialogues_session ON agent_dialogues(session_id,created_at,id)""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_agent_session_member_insert() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NOT EXISTS(SELECT 1 FROM agent_peer_sessions WHERE id=NEW.session_id AND NEW.user_id IN (left_user_id,right_user_id)) THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'session member mismatch';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER agent_session_member_insert BEFORE INSERT ON agent_session_members FOR EACH ROW EXECUTE FUNCTION avatar_trigger_agent_session_member_insert()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_agent_session_member_immutable() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'session member immutable';
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER agent_session_member_immutable BEFORE UPDATE OF session_id,user_id ON agent_session_members FOR EACH ROW EXECUTE FUNCTION avatar_trigger_agent_session_member_immutable()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_dialogue_participants_immutable() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.target_user_id IS DISTINCT FROM OLD.target_user_id
        OR (NEW.own_aic IS DISTINCT FROM OLD.own_aic AND NOT EXISTS(SELECT 1 FROM avatar_records WHERE user_id=NEW.user_id AND aic=NEW.own_aic))
        OR (NEW.target_aic IS DISTINCT FROM OLD.target_aic AND NOT EXISTS(SELECT 1 FROM avatar_records WHERE user_id=NEW.target_user_id AND aic=NEW.target_aic)) THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'participants are immutable';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER dialogue_participants_immutable BEFORE UPDATE OF user_id,own_aic,target_user_id,target_aic ON agent_dialogues FOR EACH ROW EXECUTE FUNCTION avatar_trigger_dialogue_participants_immutable()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_agent_task_member_scope() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NOT EXISTS(SELECT 1 FROM agent_dialogues WHERE id=NEW.task_id AND NEW.user_id IN (user_id,target_user_id)) THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'task member mismatch';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER agent_task_member_scope BEFORE INSERT ON agent_task_members FOR EACH ROW EXECUTE FUNCTION avatar_trigger_agent_task_member_scope()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_peer_file_scope() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NOT EXISTS(SELECT 1 FROM agent_dialogues d JOIN agent_session_members m ON m.session_id=d.session_id
        JOIN agent_task_members t ON t.task_id=d.id AND t.user_id=m.user_id
        JOIN chat_files f ON f.conversation_id=CASE WHEN d.delivery_mode='message' THEN m.resource_conversation_id ELSE t.resource_conversation_id END AND f.user_id=m.user_id
        WHERE d.id=NEW.task_id AND f.id=NEW.file_id
        AND NEW.source_user_id IN (d.user_id,d.target_user_id)) THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'peer file scope mismatch';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER peer_file_scope BEFORE INSERT ON agent_task_files FOR EACH ROW EXECUTE FUNCTION avatar_trigger_peer_file_scope()""",
)
