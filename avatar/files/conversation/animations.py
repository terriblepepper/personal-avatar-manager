"""Promote immutable animation documents into the authenticated user's material library."""
import hashlib
import re

from avatar.files.library.folders import renamed_filename, validate_path
from avatar.files.parsing.animation import parse_animation
from avatar.files.uploads.buffer import upload_work


def lookup_animation(files, user_id, conversation_id, source):
    with files.database.connect() as db:
        files.session(db, user_id, conversation_id)
        digest = hashlib.sha256(parse_animation(source).canonical()).hexdigest()
        row = files.attachments.animation_match(db, user_id, digest)
        identifier = row['id'] if row else None
    return {'attachment_id': identifier, 'already_exists': identifier is not None}


async def promote_animation(files, user, conversation_id, source, *, caption='', visibility='private', folder='', new_name=None):
    def prepare():
        with files.database.connect() as db:
            files.session(db, user['id'], conversation_id)
            scene = parse_animation(source)
            data = scene.canonical()
            existing = files.attachments.animation_match(db, user['id'], hashlib.sha256(data).hexdigest())
        return scene.title, data, existing['id'] if existing else None

    title, data, existing = await upload_work(prepare)
    validate_path(folder, root=True)
    base = re.sub(r'[\\/:*?"<>|]', '_', title).strip(' .') or '动画'
    filename = base + '.avanim'
    if new_name is not None:
        filename = renamed_filename(filename, new_name)
    if existing:
        return {'attachment_id': existing, 'already_exists': True}
    item = await files.attachments.upload(
        user, None, filename, data, caption, visibility=visibility,
        relative_path=(folder + '/' + filename) if folder else '',
        check_name=new_name is not None, authorize_animation=lambda db: files.session(db,user['id'],conversation_id),
    )
    return {'attachment_id': item['id'], 'already_exists': item['already_exists']}
