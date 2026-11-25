"""Account-scoped model metering for asynchronous callers."""
from avatar.core.db import dump, now


def record_usage(database, owner, config, usage, purpose):
    with database.connect(write=True, account_scope=owner) as db:
        db.execute(
            'INSERT INTO model_calls(user_id,provider,model,usage,created_at,purpose,caller_id) VALUES(?,?,?,?,?,?,?)',
            (owner, config['provider'], config['model'], dump(usage), now(), purpose, owner),
        )
