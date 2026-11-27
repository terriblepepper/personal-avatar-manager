"""Private knowledge workspace and its account overview."""

from avatar.knowledge.assets import KnowledgeAssetStore
from avatar.knowledge.builds import SoulBuildService
from avatar.knowledge.everos_memory import EverOSMemory
from avatar.knowledge.learning import StyleLearning
from avatar.knowledge.media import MAX_ASSET
from avatar.knowledge.question_bank import QuestionBank
from avatar.knowledge.library import folders


class KnowledgeService(SoulBuildService):
    def __init__(self, database, chat):
        assets=KnowledgeAssetStore(database,chat)
        super().__init__(assets,QuestionBank(database),StyleLearning(assets),EverOSMemory(assets))
        # Existing routes and workers use these private-asset operations on the workspace.
        self.directory=assets.directory
        self.upload_lock=assets.upload_lock
        self.asset=assets.asset
        self.file=assets.file
        self.upload_budget=assets.upload_budget
        self.upload=assets.upload
        self.store=assets.store
        self.import_material=assets.import_material
        self.edit_caption=assets.edit_caption
        self.delete_asset=assets.delete_asset
        self.batch=assets.batch

    def view(self,user_id):
        with self.database.connect() as db:
            assets=[dict(r) for r in db.execute('SELECT * FROM knowledge_assets WHERE user_id=? ORDER BY created_at DESC',(user_id,))]
            builds=[self.decode(r) for r in db.execute("SELECT * FROM soul_builds WHERE user_id=? AND origin!='learning' ORDER BY created_at DESC LIMIT 100",(user_id,))]
            active=db.execute('SELECT build_id FROM active_souls WHERE user_id=?',(user_id,)).fetchone()
            if active and not any(item['id']==active[0] for item in builds):
                builds.append(self.decode(db.execute('SELECT * FROM soul_builds WHERE id=? AND user_id=?',(active[0],user_id)).fetchone()))
            learned=db.execute('SELECT updated_at FROM style_learning_memory WHERE user_id=?',(user_id,)).fetchone()
            learning=db.execute("SELECT id,status,stage,error,updated_at FROM soul_builds WHERE user_id=? AND origin='learning' ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
            categories=[{'id':r['id'],'name':r['name'],'count':r['available']} for r in self.bank.categories(db) if r['active']]
            previous=db.execute("SELECT prompt FROM soul_builds WHERE user_id=? AND origin NOT IN ('learning','model_refresh') ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
        for item in builds:
            if item['question_plan'] and not item['bank_answers']:
                item['bank_answers']={turn['id']:{'choice':turn.get('choice','E'),'other':turn.get('other',turn['answer']) if turn.get('choice','E')=='E' else ''}
                    for turn in item['interview'] if turn['kind']=='interview' and turn.get('id') and turn.get('answer') is not None}
            for key in ('profile_snapshot','observations','interview_observations','asset_captions'):item.pop(key,None)
        return {'quota':{'used_bytes':sum(r['size'] for r in assets if not r['source_attachment_id']),'limit_bytes':MAX_ASSET},
                'folders':folders(self,user_id),'learning':{**(dict(learning) if learning else {}),'learned_at':learned[0] if learned else None},'soul_id':user_id,'assets':assets,'builds':builds,'active_build_id':active[0] if active else None,'question_categories':categories,'last_prompt':previous[0] if previous else '',
                'model':self.chat.service.models.get(user_id)['model_config']}
