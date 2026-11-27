"""Retention and immediate access checks for anonymous town conversations."""
import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException


def guest_cutoff():
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


def expired_guest(row):
    if '#guest:' not in row['peer_aic'] or row['deadline'] is not None and row['deadline'] > time.time():
        return False
    sent_at = json.loads(row['document']).get('sentAt')
    return bool(sent_at and sent_at < guest_cutoff())


def expire_guests(database, memory, batch_size):
    cutoff = guest_cutoff()
    current = time.time()
    with database.connect() as db:
        stale = db.execute("""SELECT id,user_id FROM chat_conversations WHERE platform='town-guest'
            AND updated_at<? AND lease_until<? ORDER BY updated_at,id LIMIT ?""",
            (cutoff, current, batch_size)).fetchall()
    for row in stale:
        try:
            memory.remove(row['id'], owner=row['user_id'], guest_expired_before=cutoff)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
    with database.connect(write=True) as db:
        db.execute("""DELETE FROM avatar_aip_tasks WHERE task_number IN (
            SELECT task_number FROM avatar_aip_tasks WHERE strpos(peer_aic,'#guest:')>0
            AND document::jsonb->>'sentAt'<? AND (deadline IS NULL OR deadline<=?)
            ORDER BY document::jsonb->>'sentAt' LIMIT ?)""", (cutoff, current, batch_size))
        old_requests = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        db.execute("""DELETE FROM town_visitor_requests WHERE ctid IN (
            SELECT ctid FROM town_visitor_requests WHERE created_at<? ORDER BY created_at LIMIT ?)""",
            (old_requests, batch_size))
