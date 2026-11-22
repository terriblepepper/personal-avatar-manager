"""Private per-user storage; old files are moved once before services open them."""
import hashlib
import logging
import re
import stat
from pathlib import Path


def user_root(data_dir,user_id):
    if not isinstance(user_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',user_id):raise ValueError('Invalid storage owner')
    root=Path(data_dir)/'users'/user_id
    if any(p.is_symlink() for p in (Path(data_dir),root.parent,root)):raise ValueError('Symlink storage owner')
    return root


class UserBlobs:
    def __init__(self,database,kind,table):self.database,self.kind,self.table=database,kind,table
    def folder(self,owner):
        path=user_root(self.database.data_dir,owner)/self.kind
        if path.is_symlink():raise ValueError('Symlink original directory')
        path.mkdir(parents=True,exist_ok=True,mode=0o700)
        return path
    def new(self,owner,name):
        if Path(name).name!=name:raise ValueError('Invalid original name')
        return self.folder(owner)/name
    def available(self, owner, identifier, size=None):
        """Check the authorized blob without creating directories or reading its contents."""
        if not identifier or Path(identifier).name != identifier:
            return False
        try:
            folder = user_root(self.database.data_dir, owner) / self.kind
            if folder.is_symlink():
                return False
            info = (folder / (identifier + '.blob')).lstat()
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return False
        return stat.S_ISREG(info.st_mode) and (size is None or info.st_size == size)

    def mkdir(self,**kwargs):pass
    def chmod(self,*args):pass
    def glob(self,pattern):return (self.database.data_dir/'users').glob('*/'+self.kind+'/'+pattern)
    def __truediv__(self,name):
        if Path(name).name!=name:raise ValueError('Invalid original name')
        identifier=(name[len('.removed-'):] if name.startswith('.removed-') else name)[:36]
        with self.database.connect() as db:row=db.execute('SELECT user_id FROM '+self.table+' WHERE id=?',(identifier,)).fetchone()
        if row:return self.folder(row['user_id'])/name
        matches=list(self.glob(name))
        if len(matches)>1:raise ValueError('Ambiguous original owner')
        if matches:return matches[0]
        return self.database.data_dir/'users'/'.missing'/self.kind/name


class ConversationBlobs:
    """Private originals and previews keyed by owner, conversation and file ID."""
    def __init__(self, database):
        self.database = database

    def workspace(self, owner, conversation, *, create=False):
        if not isinstance(conversation, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', conversation):
            raise ValueError('Invalid storage conversation')
        bucket = user_root(self.database.data_dir, owner) / 'chat_attachments'
        directory = bucket / conversation
        if bucket.is_symlink() or directory.is_symlink():
            raise ValueError('Symlink conversation directory')
        if create:
            bucket.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.mkdir(exist_ok=True, mode=0o700)
        return directory

    def path(self, owner, conversation, name, *, create=False):
        if Path(name).name != name or name in ('.', '..'):
            raise ValueError('Invalid original name')
        directory = self.workspace(owner, conversation, create=create)
        path = directory / name
        if path.is_symlink():
            raise ValueError('Symlink conversation original')
        return path

    def available_in(self, owner, conversation, identifier, size=None):
        try:
            info = self.path(owner, conversation, identifier + '.blob').lstat()
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return False
        return stat.S_ISREG(info.st_mode) and (size is None or info.st_size == size)

    def remove_workspace(self, owner, conversation):
        try:
            self.workspace(owner, conversation).rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # A writer or a recovered orphan may still be present. Never remove a newly opened scope.
            logging.getLogger(__name__).warning('Deferred conversation workspace cleanup')

    def glob(self):
        users = self.database.data_dir / 'users'
        if users.is_symlink():
            return
        for bucket in users.glob('*/chat_attachments'):
            if bucket.parent.is_symlink() or bucket.is_symlink():
                continue
            for path in bucket.iterdir():
                if path.is_symlink():
                    continue
                if path.is_dir():
                    yield from path.iterdir()
                else:
                    yield path


def migrate_user_storage(database):
    """Idempotent rename; never overwrite a destination or delete unowned files."""
    base=database.data_dir
    for name in ('attachments','chat_attachments','knowledge','everos','file_rag','user_skills','avatar_identities'):
        if (base/name).is_symlink():raise ValueError('Symlink legacy storage')
    with database.connect() as db:
        users=[row[0] for row in db.execute('SELECT id FROM users')]
        blobs={table:list(db.execute('SELECT id,user_id FROM '+table)) for table in ('attachments','knowledge_assets')}
        blobs['chat_files']=list(db.execute('SELECT id,user_id,conversation_id FROM chat_files'))
    conversations = ConversationBlobs(database)
    for table,kind in (('attachments','attachments'),('chat_files','chat_attachments'),('knowledge_assets','knowledge')):
        for row in blobs[table]:
            old=base/kind
            if old.is_symlink():raise ValueError('Symlink legacy originals')
            if table == 'chat_files':
                directory = conversations.workspace(row['user_id'],row['conversation_id'])
                for suffix in ('.blob','.jpg'):
                    name = row['id']+suffix
                    if Path(name).name != name:
                        raise ValueError('Invalid conversation original name')
                    for source in (old/name,directory.parent/name):
                        if source.exists() or source.is_symlink():
                            move(source,directory/name)
            else:
                for source in old.glob('*'+row['id']+'*'):
                    destination=user_root(base,row['user_id'])/kind/source.name
                    move(source,destination)
    for uid in users:
        digest=hashlib.sha256(uid.encode()).hexdigest()
        for old,new in ((base/'everos'/digest,'everos'),(base/'file_rag'/digest,'file_rag'),(base/'user_skills'/uid,'skills'),(base/'avatar_identities'/uid,'identity')):
            destination=user_root(base,uid)/new
            old_absolute=str(old.absolute());new_absolute=str(destination.absolute())
            if old.exists():move(old,destination)
            if destination.exists():
                if new=='identity':
                    for file in destination.rglob('*.json'):
                        if file.is_symlink():raise ValueError('Symlink identity configuration')
                        text=file.read_text();updated=text.replace(old_absolute,new_absolute)
                        if updated!=text:file.write_text(updated)
    for name in ('attachments','chat_attachments','knowledge','everos','file_rag','user_skills','avatar_identities'):
        old=base/name
        if old.is_dir() and not old.is_symlink():
            if not any(old.iterdir()):old.rmdir()
            else:move(old,base/'maintenance'/'unowned-storage'/name)


def move(source,destination):
    if source.is_symlink() or destination.is_symlink():raise ValueError('Symlink migration path')
    if destination.exists():raise RuntimeError('Storage migration conflict: '+str(destination))
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    source.rename(destination)
