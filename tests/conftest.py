"""所有 pytest 临时数据库和文件位于 data/test，每个用例及整次运行结束后清理。"""
from avatar.runtime.schema import install_schema
import sys
sys.dont_write_bytecode=True
import os
import shutil
import signal
from pathlib import Path
import pytest
from scripts.run_tests import TestRun,TEST_ROOT

pytest_plugins=('tests.support',)


def pytest_configure(config):
    run=TestRun('pytest');path=run.__enter__()
    config._avatar_test_run=run
    config._avatar_old_data_dir=os.environ.get('AVATAR_DATA_DIR')
    config._avatar_old_term=signal.getsignal(signal.SIGTERM)
    config.option.basetemp=str(path/'tmp')
    os.environ['AVATAR_DATA_DIR']=str(path/'default-app')
    signal.signal(signal.SIGTERM,interrupt_test)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        if 'postgres_schema' not in item.fixturenames:
            item.add_marker(pytest.mark.unit)


def interrupt_test(signum,frame):
    raise KeyboardInterrupt('测试收到终止信号，清理临时数据')


def pytest_unconfigure(config):
    run=getattr(config,'_avatar_test_run',None)
    if run is not None:
        run.__exit__()
        original=config._avatar_old_data_dir
        if original is None:os.environ.pop('AVATAR_DATA_DIR',None)
        else:os.environ['AVATAR_DATA_DIR']=original
        signal.signal(signal.SIGTERM,config._avatar_old_term)


@pytest.fixture
def tmp_path(tmp_path_factory):
    path=tmp_path_factory.mktemp('case')
    try:yield path
    finally:shutil.rmtree(path,ignore_errors=False)


@pytest.fixture
def postgres_schema():
    from tests.support import isolated_schema
    return isolated_schema


@pytest.fixture
def postgres_url(postgres_schema):
    with postgres_schema() as url:
        yield url


@pytest.fixture
def pg_database(tmp_path, postgres_url):
    from contextlib import closing
    from avatar.core.db import Database
    with closing(Database(tmp_path, postgres_url, schema=install_schema)) as database:
        database.initialize()
        yield database


@pytest.fixture(autouse=True)
def protect_runtime_database(monkeypatch):
    from avatar.core.db import Database
    from tests.support import OWNED
    original=Database.__init__
    def isolated_init(self,data_dir,database_url='',**kwargs):
        if not Path(data_dir).resolve().is_relative_to(TEST_ROOT.resolve()):
            raise RuntimeError('测试数据库只能位于 data/test')
        if database_url and database_url not in OWNED:
            raise RuntimeError('测试只允许使用本进程分配的 PostgreSQL schema')
        original(self, Path(data_dir), database_url, **kwargs)
    monkeypatch.setattr(Database,'__init__',isolated_init)


# Keep real PBKDF2 and fresh salts; reuse only identical verification work inside a test worker.
from functools import lru_cache

@lru_cache(maxsize=512)
def cached_password_hash(original,password,salt):
    return original(password,salt)


@pytest.fixture(autouse=True)
def reuse_identical_password_verification(monkeypatch):
    from avatar.accounts import auth
    original=auth.hash_password
    def hashed(password,salt=None):
        return cached_password_hash(original,password,salt or auth.secrets.token_hex(16))
    monkeypatch.setattr(auth,'hash_password',hashed)
