"""Temporary AIP workspace leases and file products that survive until acknowledgement."""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import HTTPException

from avatar.protocols.tasks.guest_maintenance import expired_guest
from avatar.core.execution import run_blocking
from avatar.files.conversation.resources import validate_output
from avatar.files.library.public import PublicMaterials


log = logging.getLogger(__name__)
NONCE = r'[0-9a-f]{32}'


class TownFiles:
    def __init__(self, chat, settings):
        self.database = chat.service.database
        self.originals = chat.service.attachments.directory
        self.scratch = settings.data_dir / 'town-workspaces'
        self.delivery = settings.data_dir / 'town-products'
        self.public_url = settings.oidc_public_url
        self.idle = []
        self.writing = set()
        self.lock = Lock()

    @staticmethod
    def empty(path):
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    @asynccontextmanager
    async def workspace(self):
        if self.scratch.is_symlink():
            raise RuntimeError('Invalid town workspace directory')
        if self.idle:
            slot = self.idle.pop()
        else:
            slot = self.scratch / ('slot-' + uuid.uuid4().hex)
            slot.mkdir(parents=True, mode=0o700)
        if slot.is_symlink() or not slot.is_dir():
            raise RuntimeError('Invalid town workspace slot')
        try:
            yield slot
        finally:
            # A canceled blocking operation drains before its directory can be lent again.
            try:
                await run_blocking(self.empty, slot)
            except OSError:
                log.error('Could not clean a temporary town workspace')
            else:
                self.idle.append(slot)

    def clear_scratch(self):
        if self.scratch.is_symlink():
            raise RuntimeError('Invalid town workspace directory')
        if self.scratch.is_dir():
            for path in self.scratch.iterdir():
                if re.fullmatch('slot-'+NONCE,path.name) and not path.is_symlink() and path.is_dir():
                    shutil.rmtree(path)
            self.idle.clear()

    def public_item(self, db, owner, item):
        row = PublicMaterials.validate(db, owner, item)
        if not self.originals.available(owner, row['id'], row['size']):
            raise HTTPException(410, '公开原件已删除')
        return row

    @staticmethod
    def open_blob(path, size):
        stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            stream.close()
            raise HTTPException(410, '原件已改变或不可用')
        return stream

    def folder(self, rowid, token):
        if not re.fullmatch(NONCE,token) or self.delivery.is_symlink():
            raise RuntimeError('Invalid town file delivery directory')
        folder = self.delivery / f'{int(rowid)}-{token}'
        if folder.is_symlink():
            raise RuntimeError('Invalid town file delivery directory')
        return folder

    def write(self, rowid, token, outputs):
        folder = self.folder(rowid, token)
        folder.mkdir(parents=True, mode=0o700)
        items = []
        for item in outputs:
            identifier = str(uuid.UUID(item['id']))
            mime = validate_output(item['filename'], item['data'])
            data = item['data']
            (folder / identifier).write_bytes(data)
            items.append({'id':identifier, 'name':item['filename'], 'mimeType':mime, 'size':len(data),
                          'sha256':hashlib.sha256(data).hexdigest()})
        return items

    @asynccontextmanager
    async def prepare(self, rowid, outputs, originals):
        if not outputs and not originals:
            yield None
            return
        token = uuid.uuid4().hex
        key = (rowid, token)
        with self.lock:
            self.writing.add(key)
        try:
            files = await run_blocking(self.write, rowid, token, outputs)
            yield {'token':token, 'files':files, 'originals':list(originals)}
        finally:
            with self.lock:
                self.writing.remove(key)
            await run_blocking(self.discard_closed, rowid, token)

    def publish(self, db, rowid, owner, delivery):
        """Bind immutable file metadata and access to the successful task transaction."""
        if delivery is None:
            return None, []
        items = list(delivery['files'])
        for item in delivery['originals']:
            row = self.public_item(db, owner, item)
            items.append({'id':item['id'], 'name':row['filename'], 'mimeType':row['content_type'],
                          'size':row['size'], 'sha256':row['sha256'], 'original':item})
        record = {'token':delivery['token'], 'files':items, 'owner':owner}
        root = self.public_url.rstrip('/') + f'/api/town/files/{rowid}/{delivery["token"]}'
        return record, [{'type':'file', 'name':item['name'], 'mimeType':item['mimeType'],
                         'uri':root+'/'+item['id'], 'metadata':{'size':item['size'], 'sha256':item['sha256'],
                            **({'inlineHtml':True} if 'original' not in item and item['mimeType']=='text/html' else {})}}
                        for item in items]

    def with_html_body(self, result):
        prefix = self.public_url.rstrip('/') + '/api/town/files/'
        for product in (result.get('products') or ()):
            items = product['dataItems']
            blocks = []
            for item in items:
                if (item['type']!='file' or not (item.get('metadata') or {}).get('inlineHtml')
                        or not item['uri'].startswith(prefix)):
                    continue
                rowid,token,identifier = item['uri'][len(prefix):].split('/')
                try:
                    source = self.download(int(rowid),token,identifier)[0]
                    with source:
                        html = source.read().decode('utf-8')
                except (HTTPException, UnicodeDecodeError):
                    continue
                fence = chr(96)*max(3,1+max((len(match.group(1)) for match in re.finditer(
                    r'(?m)^ {0,3}('+chr(96)+r'{3,})[ \t]*\r?$',html)),default=0))
                blocks.append(f'{fence}html\n{html}\n{fence}')
            if blocks:
                items[0] = {**items[0],'text':'\n\n'.join([items[0]['text'],*blocks])}
        return result

    @staticmethod
    def live(db, rowid, token):
        row = db.execute('SELECT peer_aic,document,deadline FROM avatar_aip_tasks WHERE task_number=?',(rowid,)).fetchone()
        if not row or expired_guest(row):
            return None
        document = json.loads(row['document'])
        state = document['status']['state']
        if state=='awaiting-completion':
            deadline = row['deadline']
        elif state in ('working','awaiting-input'):
            deadline = document.get('_agent_resume',{}).get('work_until')
        else:
            return None
        if deadline is None or deadline<=time.time():
            return None
        return next((delivery for delivery in document.get('_file_deliveries',()) if delivery['token']==token),None)

    def files_from_previous(self, rowid, deliveries):
        return {item['id']:{**item,'path':folder/item['id']}
                for delivery in deliveries
                for folder in (self.folder(rowid,delivery['token']),)
                for item in delivery['files']
                if 'original' not in item and (folder/item['id']).is_file()}

    def download(self, rowid, token, identifier):
        with self.database.connect() as db:
            delivery = self.live(db, rowid, token)
            if delivery and not db.execute("""SELECT 1 FROM avatar_aip_tasks t JOIN avatar_records a
                ON a.aic=t.local_aic JOIN users u ON u.id=a.user_id
                WHERE t.task_number=? AND u.id=? AND u.account_status='active' AND a.identity_status='active'""",
                (rowid,delivery['owner'])).fetchone():
                delivery = None
            item = next((file for file in delivery['files'] if file['id']==identifier), None) if delivery else None
            if not item:
                raise HTTPException(404, '任务文件不可用或已过期')
            owner = delivery['owner']
            if original := item.get('original'):
                row = self.public_item(db, owner, original)
                path = self.originals.new(owner, row['id']+'.blob')
            else:
                path = self.folder(rowid, token) / identifier
            try:
                stream = self.open_blob(path, item['size'])
            except FileNotFoundError:
                raise HTTPException(410, '任务文件已删除') from None
        return stream, item

    def discard_closed(self, rowid, token):
        folder = self.folder(rowid, token)
        if not folder.is_dir():
            return
        with self.lock:
            if (rowid, token) in self.writing:
                return
        with self.database.connect() as db:
            if self.live(db, rowid, token):
                return
        try:
            shutil.rmtree(folder)
        except FileNotFoundError:
            pass

    def discard_previous(self, rowid, deliveries):
        for delivery in deliveries:
            try:
                self.discard_closed(rowid,delivery['token'])
            except Exception:
                log.warning('Could not release a town file delivery after task settlement')

    def reap(self):
        if not self.delivery.is_dir():
            return
        for folder in self.delivery.iterdir():
            if match := re.fullmatch(r'(\d+)-('+NONCE+r')', folder.name):
                self.discard_closed(int(match[1]), match[2])

    async def janitor(self):
        while True:
            try:
                await run_blocking(self.reap)
            except Exception:
                log.exception('Could not clean stale town files')
            await asyncio.sleep(30)

