# 模块说明：账户独立模型配置，保存即可生效，不进入个人资料审核快照；Key 始终加密且不回传。
import json
import asyncio
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from avatar.core.schemas import ModelConfigInput
from avatar.profiles.profile_dates import ModifiedAt, next_modified_at
from avatar.models.model_config import public_config
from avatar.core.db import dump, event
from avatar.models.capabilities import automatic


async def verify_api(client,config,vault,*,embedding=False):
    """用待提交配置执行小请求；校验阶段不写配置、能力缓存或索引。"""
    from avatar.models.model_config import require_complete
    require_complete(config)
    try:
        async with asyncio.timeout(45):
            key=vault.decrypt(config['encrypted_key'])
            if embedding:
                from avatar.models.embedding import embed
                await embed(client,config,key,['API availability check'])
            else:
                result=await client.generate(config,key,
                    'Reply briefly to confirm the API is working.',[],'Reply OK.')
                if not result.text.strip():raise HTTPException(502,'模型没有返回可见回答')
    except TimeoutError:
        raise HTTPException(504,'API 可用性校验超时；未保存配置，未更新 RAG 索引') from None
    except HTTPException as error:
        raise HTTPException(error.status_code,'API 可用性校验失败：'+str(error.detail)+'；未保存配置，未更新 RAG 索引') from None


# 职责：定义用户独立保存模型配置的输入。
# 输入：model_config_input 为平台、模型和可选 Key，updated_at 为此前配置修改日期或首次配置时空值。
# 结果与约束：不接受其他账户 ID；配置并发冲突由账户事务检查，不改变个人资料状态或日期。
class ModelSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_config_input: ModelConfigInput
    updated_at: ModifiedAt | None = None


# 职责：管理账户当前模型设置及立即生效的加密保存。
# 输入：database 为业务数据库，vault 为现有密钥保险库。
# 结果与约束：所有问答和记忆压缩读取目标账户当前设置，审核及后台任务不覆盖用户自行保存的配置。
class ModelSettingsService:
    # 职责：组合模型设置存储与保险库。
    # 输入：database 和 vault 为共享应用实例。
    # 结果与约束：不创建新的主密钥，不调用模型或注册服务。
    def __init__(self, database, vault):
        self.database, self.vault = database, vault

    # 职责：在已有事务内读取目标所有人的当前加密模型配置。
    # 输入：db 为事务，user_id 必须由认证账户或已授权分身目标确定。
    # 结果与约束：无配置返回空对象；内部返回密文配置，仅模型协调器可解密使用。
    @staticmethod
    def current(db, user_id):
        row = db.execute('SELECT model_config FROM model_settings WHERE user_id=?', (user_id,)).fetchone()
        return automatic(db,user_id,json.loads(row['model_config'])) if row else {}

    # 职责：返回本人可显示的模型设置。
    # 输入：user_id 为已认证账户。
    # 结果与约束：只返回配置摘要及修改日期，隐藏 Key 明文和密文，不依赖个人资料是否审核。
    def get(self, user_id):
        with self.database.connect() as db:
            row = db.execute('SELECT model_config,updated_at FROM model_settings WHERE user_id=?', (user_id,)).fetchone()
            updating=bool(db.execute("SELECT 1 FROM file_rag_documents WHERE user_id=? AND status IN ('queued','running')",(user_id,)).fetchone())
            config=automatic(db,user_id,json.loads(row['model_config'])) if row else {}
        return {'model_config': public_config(config), 'updated_at': row['updated_at'] if row else None,'index_updating':updating}

    # 职责：在账户事务内校验并保存立即生效的模型配置。
    # 输入：db、user、input_config 为已认证配置请求，expected 可选核对此前配置日期。
    # 结果与约束：同平台留空可保留 Key，更换平台遵守保险库规则；不写入资料或审核快照，不产生付费调用。
    def save_in_transaction(self, db, user, input_config, expected=None, resolved=None):
        row = db.execute('SELECT model_config,updated_at FROM model_settings WHERE user_id=?', (user['id'],)).fetchone()
        if expected is not None and (not row or row['updated_at'] != expected):
            raise HTTPException(409, '模型配置已变化，请刷新后重新保存')
        config = automatic(db,user['id'],resolved if resolved is not None else self.vault.resolve(input_config, json.loads(row['model_config']) if row else {}))
        previous=json.loads(row['model_config']) if row else {}
        if any(previous.get(key)!=config.get(key) for key in ('provider','base_url','model','vision_enabled','audio_enabled','encrypted_key')):
            from avatar.rag.invalidation import invalidate
            invalidate(db,user['id'])
        modified = next_modified_at(row['updated_at'] if row else '')
        db.execute('INSERT INTO model_settings(user_id,model_config,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET model_config=excluded.model_config,updated_at=excluded.updated_at', (user['id'],dump(config),modified))
        event(db,user['id'],user['username'],'保存模型配置（立即生效）', actor_id=user['id'])

    # 职责：独立保存用户模型配置并返回可显示结果。
    # 输入：user 为当前账户，request 为 ModelSettingsRequest。
    # 结果与约束：资料处于待审核或执行状态时也可保存，既有审核通过任务不会回滚新 Key；修改仅影响后续模型请求。
    def save(self, user, request, *, validated=None, baseline=None):
        with self.database.connect(write=True) as db:
            if validated is not None:
                row=db.execute('SELECT updated_at FROM model_settings WHERE user_id=?',(user['id'],)).fetchone()
                if (row[0] if row else None)!=baseline:raise HTTPException(409,'校验期间模型配置已变化，请刷新后重新保存')
            self.save_in_transaction(db,user,request.model_config_input,request.updated_at,resolved=validated)
        return self.get(user['id'])
