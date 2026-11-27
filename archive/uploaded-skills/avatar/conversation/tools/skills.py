# 模块说明：会话内技能移出/恢复与实际使用记录，不改变用户安装目录或全局启用状态。
from fastapi import HTTPException
from avatar.core.db import now
from avatar.skills.policy import SKILLS_PAUSED, require_skills_enabled


# 职责：管理本人 Web 父会话的技能集合，作为上下文过滤与界面展示的共同来源。
# 输入：database 为应用数据库。
# 结果与约束：只处理会话所有人的技能，不接触他人的技能正文或模型配置。
class SessionSkills:
    # 职责：保存数据库依赖，不创建或加载技能。
    def __init__(self,database):self.database=database

    # 职责：核验当前用户拥有指定 Web 父会话。
    # 输入：事务、已认证账户 ID 与会话 ID。
    # 结果与约束：他人、内部子会话及不存在记录统一返回 404。
    def owned(self,db,user_id,conversation_id):
        row=db.execute("SELECT * FROM chat_conversations WHERE id=? AND user_id=? AND platform='web' AND parent_id IS NULL",(conversation_id,user_id)).fetchone()
        if not row:raise HTTPException(404,'会话不存在')
        return row

    # 职责：读取会话实际用过或已移出的技能元数据。
    # 输入：已认证账户及本人父会话 ID。
    # 结果与约束：只返回名称、启用状态与使用日期，不返回正文；全局卸载后的历史回答快照仍保留。
    def list(self,user_id,conversation_id):
        with self.database.connect() as db:
            self.owned(db,user_id,conversation_id)
            if SKILLS_PAUSED:
                return []
            rows=db.execute('SELECT s.id,s.name,s.enabled AS installed_enabled,(CASE WHEN c.enabled=1 AND c.authorized=1 THEN 1 ELSE 0 END) AS enabled,c.last_used_at FROM chat_session_skills c JOIN agent_skills s ON s.id=c.skill_id WHERE c.conversation_id=? AND s.user_id=? ORDER BY c.enabled,c.last_used_at DESC,s.name',(conversation_id,user_id)).fetchall()
        return [dict(row) for row in rows]

    # 职责：在上下文加载前读取会话停用技能及本地集合日期。
    # 输入：事务、本人父会话 ID。
    # 结果与约束：返回 ID 集合和日期，后续只从已验证的本人技能目录过滤。
    def excluded(self,db,conversation_id):
        if SKILLS_PAUSED:
            return set()
        return {row['skill_id'] for row in db.execute('SELECT skill_id FROM chat_session_skills WHERE conversation_id=? AND enabled=0',(conversation_id,))}

    # 职责：读取本人会话持续使用的技能，最近使用优先；全局停用、卸载或会话移出立即失效。
    # 结果与约束：只返回当前账户安装且启用的 ID，不携带其他用户的技能或历史正文。
    def active(self,db,user_id,conversation_id):
        if SKILLS_PAUSED:
            return []
        return [r['skill_id'] for r in db.execute('SELECT c.skill_id FROM chat_session_skills c JOIN agent_skills s ON s.id=c.skill_id WHERE c.conversation_id=? AND s.user_id=? AND c.enabled=1 AND c.authorized=1 AND s.enabled=1 ORDER BY c.last_used_at DESC,c.skill_id',(conversation_id,user_id))]

    # 职责：只在指定会话移出或恢复已安装技能。
    # 输入：用户 ID、会话、本人技能 ID 及布尔启用值，调用者应持有会话租约。
    # 结果与约束：不删除安装文件或全局配置，清除旧摘要以避免沿用先前技能指令。
    def set_enabled(self,user_id,conversation_id,skill_id,enabled):
        require_skills_enabled()
        with self.database.connect(write=True) as db:
            self.owned(db,user_id,conversation_id)
            skill=db.execute('SELECT enabled FROM agent_skills WHERE id=? AND user_id=?',(skill_id,user_id)).fetchone()
            if not skill:raise HTTPException(404,'技能不存在或已卸载')
            if enabled and not skill['enabled']:raise HTTPException(409,'请先在分身技能管理中启用此技能')
            db.execute('INSERT INTO chat_session_skills(conversation_id,skill_id,enabled,authorized) VALUES(?,?,?,1) ON CONFLICT(conversation_id,skill_id) DO UPDATE SET enabled=excluded.enabled,authorized=1',(conversation_id,skill_id,int(enabled)))
            db.execute("UPDATE chat_conversations SET skills_updated_at=?,summary='',summary_through=0,summary_profile_modified_at='' WHERE id=?",(now(),conversation_id))
        return self.list(user_id,conversation_id)

    # 职责：与回答同事务记录实际加载技能，不把目录候选当作已执行。
    # 输入：写事务、认证账户、父会话 ID 与本轮有限技能元数据。
    # 结果与约束：再次核对所有人；停用状态不会因旧回答回写而重新启用。
    def used(self,db,user_id,conversation_id,skills):
        if SKILLS_PAUSED:
            return
        self.owned(db,user_id,conversation_id)
        for skill in skills:
            if db.execute('SELECT 1 FROM agent_skills WHERE id=? AND user_id=?',(skill['id'],user_id)).fetchone():
                db.execute('INSERT INTO chat_session_skills(conversation_id,skill_id,last_used_at,authorized) VALUES(?,?,?,1) ON CONFLICT(conversation_id,skill_id) DO UPDATE SET last_used_at=excluded.last_used_at,authorized=1',(conversation_id,skill['id'],now()))
