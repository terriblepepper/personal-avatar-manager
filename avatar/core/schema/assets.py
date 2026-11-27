"""Assets tables, indexes and triggers."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS rag_lexical_records(
      user_id TEXT NOT NULL,collection TEXT NOT NULL,record_id TEXT NOT NULL,
      file_id TEXT NOT NULL,source TEXT NOT NULL,text TEXT NOT NULL,metadata TEXT NOT NULL,tokens TEXT NOT NULL,
      UNIQUE(user_id,collection,record_id))""",
    """CREATE TABLE IF NOT EXISTS soul_question_categories (
      id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,active BIGINT NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS agent_skill_collections (
      user_id TEXT PRIMARY KEY REFERENCES users(id), updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS agent_skills (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
      description TEXT NOT NULL, instructions TEXT NOT NULL, manifest TEXT NOT NULL,
      enabled BIGINT NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL, UNIQUE(user_id,name))""",
    """CREATE TABLE IF NOT EXISTS attachment_collections (
              user_id TEXT PRIMARY KEY REFERENCES users(id),updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS attachments (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), filename TEXT NOT NULL,
              content_type TEXT NOT NULL, size BIGINT NOT NULL, sha256 TEXT NOT NULL,
              extracted_text TEXT NOT NULL DEFAULT '', extraction_status TEXT NOT NULL, created_at TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '', saved_at TEXT, visibility TEXT NOT NULL DEFAULT 'private', updated_at TEXT NOT NULL DEFAULT '', relative_path TEXT NOT NULL DEFAULT '', visibility_override BIGINT NOT NULL DEFAULT 0, extraction_version BIGINT NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS embedding_settings (
      user_id TEXT PRIMARY KEY REFERENCES users(id),config TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS file_rag_documents(
      source TEXT NOT NULL CHECK(source IN ('profile','chat')),file_id TEXT NOT NULL,
      user_id TEXT NOT NULL REFERENCES users(id),status TEXT NOT NULL DEFAULT 'queued',
      revision TEXT NOT NULL DEFAULT '',signature TEXT NOT NULL DEFAULT '',coverage TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT '', lease_until DOUBLE PRECISION NOT NULL DEFAULT 0, next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0, generation BIGINT NOT NULL DEFAULT 0, cache_epoch TEXT NOT NULL DEFAULT '', force_rebuild BIGINT NOT NULL DEFAULT 0, collection TEXT NOT NULL DEFAULT '', active_publication TEXT NOT NULL DEFAULT '', legacy_backfill BIGINT NOT NULL DEFAULT 0,PRIMARY KEY(source,file_id))""",
    """CREATE TABLE IF NOT EXISTS file_rag_epochs(user_id TEXT PRIMARY KEY REFERENCES users(id),epoch TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS file_rag_gc(
      user_id TEXT NOT NULL REFERENCES users(id),source TEXT NOT NULL,file_id TEXT NOT NULL,
      collection TEXT NOT NULL,record_id TEXT NOT NULL,delete_after DOUBLE PRECISION NOT NULL,
      PRIMARY KEY(user_id,collection,record_id))""",
    """CREATE TABLE IF NOT EXISTS knowledge_asset_updates(user_id TEXT PRIMARY KEY REFERENCES users(id),revision TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_assets (
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),filename TEXT NOT NULL,
      mime TEXT NOT NULL,size BIGINT NOT NULL,sha256 TEXT NOT NULL,kind TEXT NOT NULL,
      caption TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL, source_attachment_id TEXT NOT NULL DEFAULT '', relative_path TEXT NOT NULL DEFAULT '',UNIQUE(user_id,sha256))""",
    """CREATE TABLE IF NOT EXISTS knowledge_collections(user_id TEXT PRIMARY KEY REFERENCES users(id),updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_folders(user_id TEXT NOT NULL REFERENCES users(id),path TEXT NOT NULL,caption TEXT NOT NULL DEFAULT '',PRIMARY KEY(user_id,path))""",
    """CREATE TABLE IF NOT EXISTS material_folders (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,path TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '', visibility TEXT,
              PRIMARY KEY(user_id,path))""",
    """CREATE TABLE IF NOT EXISTS model_artifact_cache(
      user_id TEXT NOT NULL REFERENCES users(id),kind TEXT NOT NULL,signature TEXT NOT NULL,digest TEXT NOT NULL,
      source_hash TEXT NOT NULL DEFAULT '',value BYTEA NOT NULL,updated_at TEXT NOT NULL,
      PRIMARY KEY(user_id,kind,signature,digest))""",
    """CREATE TABLE IF NOT EXISTS model_calls (
              id BIGSERIAL PRIMARY KEY, user_id TEXT REFERENCES users(id),
              provider TEXT NOT NULL, model TEXT NOT NULL, usage TEXT NOT NULL,
              created_at TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT 'answer', caller_id TEXT, conversation_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS model_settings (
              user_id TEXT PRIMARY KEY REFERENCES users(id),model_config TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS rag_schedule_users(user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,last_served DOUBLE PRECISION NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS soul_builds (
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),prompt TEXT NOT NULL,
      asset_ids TEXT NOT NULL,profile_snapshot TEXT NOT NULL,profile_revision TEXT NOT NULL,
      status TEXT NOT NULL,progress BIGINT NOT NULL DEFAULT 0,stage TEXT NOT NULL,
      notes TEXT NOT NULL DEFAULT '[]',document TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',
      reviewer TEXT REFERENCES users(id),reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,reviewed_at TEXT, interview_rounds BIGINT NOT NULL DEFAULT 0, interview TEXT NOT NULL DEFAULT '[]', observations TEXT NOT NULL DEFAULT '[]', phase TEXT NOT NULL DEFAULT 'analysis', asset_captions TEXT NOT NULL DEFAULT '{}', question_plan TEXT NOT NULL DEFAULT '[]', interview_observations TEXT NOT NULL DEFAULT '[]', analyzed_answers BIGINT NOT NULL DEFAULT 0, bank_answers TEXT NOT NULL DEFAULT '{}', followup_rounds BIGINT NOT NULL DEFAULT 0, parent_build TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'build', memory_context TEXT NOT NULL DEFAULT '{}', memory_fingerprint TEXT NOT NULL DEFAULT '', memory_tree TEXT NOT NULL DEFAULT '{}', chat_samples TEXT NOT NULL DEFAULT '[]', rebuild BIGINT NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS soul_questions (
      id TEXT PRIMARY KEY,category_id TEXT NOT NULL REFERENCES soul_question_categories(id),
      prompt TEXT NOT NULL,fingerprint TEXT NOT NULL UNIQUE,source TEXT NOT NULL,
      active BIGINT NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS style_learning_memory (
      user_id TEXT PRIMARY KEY REFERENCES users(id),document TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS style_learning_runs (
      build_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),mode TEXT NOT NULL,
      day TEXT NOT NULL,sources TEXT NOT NULL,samples TEXT NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS style_learning_settings (
      conversation_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),
      enabled BIGINT NOT NULL DEFAULT 0,last_message_id BIGINT NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS active_souls (
      user_id TEXT PRIMARY KEY REFERENCES users(id),build_id TEXT NOT NULL REFERENCES soul_builds(id))""",
    """CREATE TABLE IF NOT EXISTS file_rag_nodes(
      source TEXT NOT NULL,file_id TEXT NOT NULL,user_id TEXT NOT NULL REFERENCES users(id),
      ordinal BIGINT NOT NULL,collection TEXT NOT NULL,record_id TEXT NOT NULL,
      PRIMARY KEY(source,file_id,ordinal),
      FOREIGN KEY(source,file_id) REFERENCES file_rag_documents(source,file_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS rag_publications(
      id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),source TEXT NOT NULL,file_id TEXT NOT NULL,
      revision TEXT NOT NULL,signature TEXT NOT NULL,collection TEXT NOT NULL,coverage TEXT NOT NULL,
      record_ids TEXT NOT NULL,manifest TEXT NOT NULL,lexical_ready BIGINT NOT NULL DEFAULT 0,
      legacy BIGINT NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
      FOREIGN KEY(source,file_id) REFERENCES file_rag_documents(source,file_id) ON DELETE CASCADE)""",
    """CREATE INDEX IF NOT EXISTS rag_owner_state ON file_rag_documents(user_id,status)""",
    """CREATE INDEX IF NOT EXISTS rag_gc_due ON file_rag_gc(delete_after)""",
    """CREATE INDEX IF NOT EXISTS rag_nodes_record ON file_rag_nodes(user_id,collection,record_id)""",
    """CREATE INDEX IF NOT EXISTS rag_publication_owner ON rag_publications(user_id,collection)""",
    """CREATE INDEX IF NOT EXISTS soul_builds_queue ON soul_builds(status,created_at)""",
    """CREATE INDEX IF NOT EXISTS soul_builds_owner ON soul_builds(user_id,created_at)""",
    """CREATE INDEX IF NOT EXISTS soul_questions_category ON soul_questions(category_id,active)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS style_learning_daily ON style_learning_runs(user_id,day) WHERE mode='auto'""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_skill_owner_immutable() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owner is immutable';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER skill_owner_immutable BEFORE UPDATE OF user_id ON agent_skills FOR EACH ROW EXECUTE FUNCTION avatar_trigger_skill_owner_immutable()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_rag_profile_insert() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
INSERT INTO file_rag_documents(source,file_id,user_id,cache_epoch) VALUES('profile',NEW.id,NEW.user_id,coalesce((SELECT epoch FROM file_rag_epochs WHERE user_id=NEW.user_id),''));
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER rag_profile_insert AFTER INSERT ON attachments FOR EACH ROW EXECUTE FUNCTION avatar_trigger_rag_profile_insert()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_rag_profile_update() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NEW.caption!=OLD.caption OR NEW.sha256!=OLD.sha256 OR NEW.filename!=OLD.filename OR NEW.relative_path!=OLD.relative_path THEN
UPDATE file_rag_documents SET status='queued',error='',generation=generation+1 WHERE source='profile' AND file_id=NEW.id;
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER rag_profile_update AFTER UPDATE OF caption,sha256,filename,relative_path ON attachments FOR EACH ROW EXECUTE FUNCTION avatar_trigger_rag_profile_update()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_rag_profile_delete() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
DELETE FROM file_rag_documents WHERE source='profile' AND file_id=OLD.id;
RETURN OLD; END $avatar$""",
    """CREATE OR REPLACE TRIGGER rag_profile_delete AFTER DELETE ON attachments FOR EACH ROW EXECUTE FUNCTION avatar_trigger_rag_profile_delete()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_attachments_parse_cache_delete() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
DELETE FROM model_artifact_cache WHERE user_id=OLD.user_id AND kind='parse' AND source_hash=OLD.sha256
          AND NOT EXISTS(SELECT 1 FROM attachments WHERE user_id=OLD.user_id AND sha256=OLD.sha256)
          AND NOT EXISTS(SELECT 1 FROM chat_files WHERE user_id=OLD.user_id AND sha256=OLD.sha256);
RETURN OLD; END $avatar$""",
    """CREATE OR REPLACE TRIGGER attachments_parse_cache_delete AFTER DELETE ON attachments FOR EACH ROW EXECUTE FUNCTION avatar_trigger_attachments_parse_cache_delete()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_rag_document_delete() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
INSERT INTO file_rag_gc(user_id,source,file_id,collection,record_id,delete_after)
        SELECT user_id,source,file_id,collection,record_id,0 FROM file_rag_nodes WHERE source=OLD.source AND file_id=OLD.file_id ON CONFLICT(user_id,collection,record_id) DO UPDATE SET source=EXCLUDED.source,file_id=EXCLUDED.file_id,delete_after=EXCLUDED.delete_after;
      UPDATE file_rag_gc SET delete_after=0 WHERE user_id=OLD.user_id AND source=OLD.source AND file_id=OLD.file_id;
RETURN OLD; END $avatar$""",
    """CREATE OR REPLACE TRIGGER rag_document_delete BEFORE DELETE ON file_rag_documents FOR EACH ROW EXECUTE FUNCTION avatar_trigger_rag_document_delete()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_knowledge_assets_owner_immutable() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owner is immutable';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER knowledge_assets_owner_immutable BEFORE UPDATE OF user_id ON knowledge_assets FOR EACH ROW EXECUTE FUNCTION avatar_trigger_knowledge_assets_owner_immutable()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_soul_builds_owner_immutable() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owner is immutable';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER soul_builds_owner_immutable BEFORE UPDATE OF user_id ON soul_builds FOR EACH ROW EXECUTE FUNCTION avatar_trigger_soul_builds_owner_immutable()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_active_soul_owner_insert() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NOT EXISTS(SELECT 1 FROM soul_builds b WHERE b.id=NEW.build_id AND b.user_id=NEW.user_id) THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'soul owner mismatch';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER active_soul_owner_insert BEFORE INSERT ON active_souls FOR EACH ROW EXECUTE FUNCTION avatar_trigger_active_soul_owner_insert()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_active_soul_owner_update() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NOT EXISTS(SELECT 1 FROM soul_builds b WHERE b.id=NEW.build_id AND b.user_id=NEW.user_id) THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'soul owner mismatch';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER active_soul_owner_update BEFORE UPDATE ON active_souls FOR EACH ROW EXECUTE FUNCTION avatar_trigger_active_soul_owner_update()""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_attachment_owner_immutable() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owner is immutable';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER attachment_owner_immutable BEFORE UPDATE OF user_id ON attachments FOR EACH ROW EXECUTE FUNCTION avatar_trigger_attachment_owner_immutable()""",
)
