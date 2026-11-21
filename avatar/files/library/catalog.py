"""Compact, query-aware projections for model prompts. User-facing metadata stays complete."""
import json

from avatar.core.db import dump
from avatar.core.tokens import estimate


def model_animation_metadata(metadata, query=''):
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if not metadata or not metadata.get('title'):
        return {}
    needle = query.casefold()
    tags = sorted(metadata.get('tags', []), key=lambda tag: not _mentioned(tag, needle))
    return {'title':metadata['title'][:96], 'tags':[tag[:32] for tag in tags[:4]], 'durationMs':metadata['durationMs']}


def _mentioned(text, needle):
    text = text.casefold()
    return bool(text and needle and (text in needle or needle in text))


def fit_catalog(rows, query, budget):
    """Prefer named files and explicit conversation references when the catalog won't fit."""
    if budget <= 1:
        return []
    needle = query.casefold()
    def priority(row):
        fields = (row.get('id',''),row.get('file_id',''),row.get('filename',''),row.get('relative_path',''),
                  row.get('path',''),row.get('title',''),*row.get('tags',[]))
        return (any(_mentioned(field,needle) for field in fields), row.get('source')=='chat')
    remaining = budget - estimate('[]')
    kept = []
    for row in sorted(rows,key=priority,reverse=True):
        cost = estimate(dump(row)+', ')
        if cost<=remaining:
            kept.append(row)
            remaining-=cost
    return kept
