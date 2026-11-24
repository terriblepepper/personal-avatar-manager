"""独立知识库素材、后台构建和审核后生效版本。"""


def restore_asset_sources(db):
    """来源列首次引入时，按所有者和 SHA 匹配历史附件。"""
    db.execute("""UPDATE knowledge_assets SET source_attachment_id=coalesce(
        (SELECT a.id FROM attachments a WHERE a.user_id=knowledge_assets.user_id
         AND a.sha256=knowledge_assets.sha256 ORDER BY a.created_at,a.id LIMIT 1),'')""")
