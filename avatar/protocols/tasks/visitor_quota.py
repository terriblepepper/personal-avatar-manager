"""Per-account daily admission limits for public town conversations."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

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

