"""Platform policy for user-uploaded skill packages."""
from fastapi import HTTPException

MAX_UPLOAD = 50 * 1024 * 1024
SKILLS_PAUSED = True
PAUSE_NOTICE = '因安全问题暂时停止分身技能的使用'


def require_skills_enabled():
    if SKILLS_PAUSED:
        raise HTTPException(403, PAUSE_NOTICE)
