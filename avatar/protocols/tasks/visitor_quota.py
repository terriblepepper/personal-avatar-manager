"""Per-account daily admission limits for public town conversations."""
import time
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from avatar.core.db import now
from avatar.protocols.tasks.store import ManagementTaskError


DailyLimit = Annotated[int, Field(strict=True, ge=0, le=2**53-1)]


class TownAccessLimits(BaseModel):
    model_config = ConfigDict(extra='forbid')

    visitor_daily_limit: DailyLimit | None
    total_daily_limit: DailyLimit | None


def get_limits(db, owner):
    row = db.execute('SELECT visitor_daily_limit,total_daily_limit FROM town_access_limits WHERE owner_id=?',
                     (owner,)).fetchone()
    if row:
        return TownAccessLimits(**dict(row))
    return TownAccessLimits(visitor_daily_limit=None, total_daily_limit=None)


def save_limits(db, owner, limits):
    db.execute('''INSERT INTO town_access_limits(owner_id,visitor_daily_limit,total_daily_limit) VALUES(?,?,?)
                  ON CONFLICT(owner_id) DO UPDATE SET visitor_daily_limit=excluded.visitor_daily_limit,
                      total_daily_limit=excluded.total_daily_limit''',
               (owner,limits.visitor_daily_limit,limits.total_daily_limit))


def check_quota(db, owner, visitor, day):
    limits = get_limits(db, owner)
    visitor_limit, total_limit = limits.visitor_daily_limit, limits.total_daily_limit
    if visitor_limit is None and total_limit is None:
        return
    if 0 in (visitor_limit,total_limit):
        raise ManagementTaskError(-32010, '今日公开对话额度已用完')
    usage = db.execute('''SELECT count(*) FILTER (WHERE visitor_id=?) AS visitor,count(*) AS total
                          FROM town_visitor_requests WHERE owner_id=? AND created_at>=?''',
                       (visitor,owner,day)).fetchone()
    if ((visitor_limit is not None and usage['visitor']>=visitor_limit)
            or (total_limit is not None and usage['total']>=total_limit)):
        raise ManagementTaskError(-32010, '今日公开对话额度已用完')



def charge_visitor(db, owner, aic, scope_peer, visitor):
    timestamp = now()
    check_quota(db, owner, visitor, timestamp[:10])
    if visitor_busy(db, aic, scope_peer):
        raise ManagementTaskError(-32010, '该访客已有请求正在处理')
    db.execute('INSERT INTO town_visitor_requests VALUES(?,?,?)', (owner, visitor, timestamp))


def visitor_busy(db, aic, peer):
    return db.execute("""SELECT 1 FROM avatar_aip_tasks WHERE local_aic=? AND peer_aic=?
        AND (document::jsonb->'status'->>'state')='working' AND deadline>? LIMIT 1""",
        (aic,peer,time.time())).fetchone() is not None
