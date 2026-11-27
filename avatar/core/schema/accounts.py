"""Accounts tables, indexes and triggers."""

STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS data_migrations(name TEXT PRIMARY KEY,applied_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS login_limits (
              key TEXT PRIMARY KEY, attempts BIGINT NOT NULL, reset_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS oidc_login_states (
              state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
              verifier TEXT NOT NULL, nonce TEXT NOT NULL,
              person_type TEXT NOT NULL, return_to TEXT NOT NULL,
              platform_token TEXT, expires DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS recovery_limits (key TEXT PRIMARY KEY, attempts BIGINT NOT NULL, reset_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
              password TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL, is_root BIGINT NOT NULL DEFAULT 0, person_type TEXT NOT NULL DEFAULT 'student', student_id TEXT, teacher_id TEXT, account_status TEXT NOT NULL DEFAULT 'active', account_reason TEXT NOT NULL DEFAULT '', email TEXT, phone TEXT)""",
    """CREATE TABLE IF NOT EXISTS account_reviews (
              id BIGSERIAL PRIMARY KEY, user_id TEXT REFERENCES users(id), reviewer TEXT REFERENCES users(id),
              approved BIGINT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS audit_events (
              id BIGSERIAL PRIMARY KEY, user_id TEXT REFERENCES users(id),
              actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL, actor_id TEXT REFERENCES users(id) ON DELETE SET NULL)""",
    """CREATE TABLE IF NOT EXISTS avatar_entity_ports (
      user_id TEXT PRIMARY KEY REFERENCES users(id), port BIGINT NOT NULL UNIQUE,
      CHECK(port BETWEEN 1024 AND 65535))""",
    """CREATE TABLE IF NOT EXISTS avatar_identity_changes (
      old_aic TEXT PRIMARY KEY, new_aic TEXT NOT NULL UNIQUE,
      user_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS avatar_identity_jobs (
      id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
      actor TEXT NOT NULL, action TEXT NOT NULL, expected_aic TEXT NOT NULL,
      status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', attempts BIGINT NOT NULL DEFAULT 0,
      next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, remote_aic TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS avatar_records (
              user_id TEXT PRIMARY KEY REFERENCES users(id), aic TEXT UNIQUE NOT NULL,
              certificate TEXT NOT NULL, profile TEXT NOT NULL, last_modified_at TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, acs_input TEXT NOT NULL DEFAULT '{}', acs_document TEXT NOT NULL DEFAULT '{}', identity_status TEXT NOT NULL DEFAULT 'active')""",
    """CREATE TABLE IF NOT EXISTS identity_migration_previews (
              token_hash TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES users(id),
              user_id TEXT NOT NULL REFERENCES users(id),subject TEXT NOT NULL,
              allow_empty_transfer BIGINT NOT NULL,issuer TEXT NOT NULL,
              fingerprint TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS login_devices (
              user_id TEXT NOT NULL REFERENCES users(id),device_hash TEXT NOT NULL,
              os TEXT NOT NULL,browser TEXT NOT NULL,webview TEXT NOT NULL,ip TEXT NOT NULL,
              first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,PRIMARY KEY(user_id,device_hash))""",
    """CREATE TABLE IF NOT EXISTS oidc_identities (
              issuer TEXT NOT NULL, subject TEXT NOT NULL,
              user_id TEXT NOT NULL UNIQUE REFERENCES users(id),
              phone TEXT UNIQUE, verified_at TEXT, username TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(issuer,subject))""",
    """CREATE TABLE IF NOT EXISTS oidc_identity_links (
              issuer TEXT NOT NULL, subject TEXT NOT NULL,
              user_id TEXT NOT NULL REFERENCES users(id), username TEXT NOT NULL DEFAULT '',
              town_principal_id TEXT, evidence TEXT NOT NULL, verified_at TEXT,
              PRIMARY KEY(issuer,subject), UNIQUE(user_id,issuer))""",
    """CREATE TABLE IF NOT EXISTS platform_bindings (
              platform TEXT NOT NULL, account_id TEXT NOT NULL,
              user_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
              PRIMARY KEY(platform,account_id), UNIQUE(platform,user_id))""",
    """CREATE TABLE IF NOT EXISTS platform_entry_tokens (
              token_hash TEXT PRIMARY KEY, platform TEXT NOT NULL, account_id TEXT NOT NULL,
              issued_for_user_id TEXT REFERENCES users(id), expires DOUBLE PRECISION NOT NULL, used_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS profile_records (
              user_id TEXT PRIMARY KEY REFERENCES users(id), draft TEXT NOT NULL DEFAULT '{}',
              last_modified_at TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft',
              reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, acs_input TEXT NOT NULL DEFAULT '{}', acs_document TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS profile_submissions (
              id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), last_modified_at TEXT NOT NULL,
              snapshot TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
              reviewer TEXT REFERENCES users(id), submitted_at TEXT NOT NULL, reviewed_at TEXT, acs_input TEXT NOT NULL DEFAULT '{}', acs_document TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS record_dismissals (
      user_id TEXT NOT NULL REFERENCES users(id), collection TEXT NOT NULL,
      record_id TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(user_id,collection,record_id))""",
    """CREATE TABLE IF NOT EXISTS sessions (
              token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id),
              csrf TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL, auth_method TEXT NOT NULL DEFAULT 'local', access_scope TEXT NOT NULL DEFAULT 'full')""",
    """CREATE TABLE IF NOT EXISTS provision_jobs (
              id TEXT PRIMARY KEY, submission_id TEXT UNIQUE REFERENCES profile_submissions(id),
              status TEXT NOT NULL, attempts BIGINT NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0, registration_phase TEXT NOT NULL DEFAULT 'queued')""",
    """CREATE INDEX IF NOT EXISTS submission_owner_time ON profile_submissions(user_id,submitted_at)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS single_root_account ON users(is_root) WHERE is_root=1""",
    """CREATE UNIQUE INDEX IF NOT EXISTS unique_account_email ON users(email) WHERE email IS NOT NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS unique_account_phone ON users(phone) WHERE phone IS NOT NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS avatar_identity_job_busy ON avatar_identity_jobs(user_id)
      WHERE status IN ('queued','running','waiting')""",
    """CREATE INDEX IF NOT EXISTS audit_actor_time ON audit_events(actor_id,id)""",
    """CREATE TABLE IF NOT EXISTS avatar_schema_version(version BIGINT PRIMARY KEY)""",
    """INSERT INTO avatar_schema_version(version) VALUES (1) ON CONFLICT DO NOTHING""",
    """CREATE OR REPLACE FUNCTION avatar_trigger_avatar_owner_immutable() RETURNS trigger LANGUAGE plpgsql AS $avatar$ BEGIN
IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'owner is immutable';
END IF;
RETURN NEW; END $avatar$""",
    """CREATE OR REPLACE TRIGGER avatar_owner_immutable BEFORE UPDATE OF user_id ON avatar_records FOR EACH ROW EXECUTE FUNCTION avatar_trigger_avatar_owner_immutable()""",
)
