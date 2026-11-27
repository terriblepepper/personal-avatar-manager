"""Private per-user storage."""
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
