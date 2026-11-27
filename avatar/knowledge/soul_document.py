"""Fields and public projection of a saved Soul document."""
import json

FIELDS = ('voice','personality','expression','appearance','boundaries','examples','uncertainties')
LABELS = {'voice':'语气与声音表达','personality':'互动风格','expression':'措辞与表达习惯',
          'appearance':'形象呈现偏好','boundaries':'表达边界','examples':'表达示例','uncertainties':'待确认事项'}


def decode_build(row):
    value=dict(row)
    for key in ('asset_ids','profile_snapshot','notes','document','interview','observations','asset_captions','question_plan','interview_observations','bank_answers','memory_tree','chat_samples'):value[key]=json.loads(value[key])
    return value


def approved_style(db, user_id, *, build_id=None):
    if build_id is None:
        row=db.execute("SELECT b.document FROM active_souls a JOIN soul_builds b ON b.id=a.build_id WHERE a.user_id=? AND b.user_id=? AND b.status='approved'",(user_id,user_id)).fetchone()
    else:
        row=db.execute("SELECT document FROM soul_builds WHERE id=? AND user_id=? AND status='approved'",(build_id,user_id)).fetchone()
    if not row:return None
    doc=json.loads(row['document'])
    return {key:doc.get(key,'') for key in FIELDS if key!='uncertainties'}
