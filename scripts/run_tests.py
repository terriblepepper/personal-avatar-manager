"""Small isolated parallel smoke-test runner."""
import argparse
import fcntl
import json
import os
import shutil
import tempfile
from pathlib import Path
import signal
import subprocess
import sys
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


ROOT=Path(__file__).resolve().parent.parent
TEST_ROOT=ROOT/'data/test'
MARKER='.test-run.json'


def process_start(pid):
    """读取 Linux 进程启动标识，区分存活进程与复用的 PID。"""
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return fields[19]
    except (OSError,IndexError):
        return None


def remove_run(path):
    """只允许删除测试根目录的已标记直接子目录，拒绝路径越界或符号链接。"""
    path=Path(path)
    if path.is_symlink() or path.parent.resolve()!=TEST_ROOT.resolve() or not path.name.startswith('run-'):
        raise RuntimeError('拒绝清理不属于 data/test 的运行目录')
    if path.exists():
        if not (path/MARKER).is_file():raise RuntimeError('测试目录缺少运行标记，拒绝删除')
        shutil.rmtree(path)


def clean_stale_runs():
    """回收前次强制终止留下的已标记目录，保留仍在运行的测试。"""
    if TEST_ROOT.is_symlink():raise RuntimeError('data/test 不能是符号链接')
    TEST_ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);TEST_ROOT.chmod(0o700)
    for path in TEST_ROOT.glob('run-*'):
        if path.is_symlink():continue
        try:
            owner=json.loads((path/MARKER).read_text())
            pid=int(owner['pid']);start=str(owner['process_start'])
        except (OSError,ValueError,KeyError,TypeError):
            continue
        # Old markers cannot establish ownership across PID namespaces.
        if owner.get('lock_protocol') != 1:
            continue
        try:
            with (path / MARKER).open('r+') as marker:
                try:
                    fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if process_start(pid) != start:
                    remove_run(path)
        except FileNotFoundError:
            continue


class TestRun:
    """为一次测试分配私有目录；成功、失败及异常退出时删除全部产物。"""
    __test__=False

    def __init__(self,kind):
        if not kind.isascii() or not kind.isalpha():raise ValueError('测试类型须为 ASCII 字母')
        self.kind,self.path=kind,None
        self.marker = None

    def __enter__(self):
        clean_stale_runs()
        self.path=Path(tempfile.mkdtemp(prefix='run-'+self.kind+'-',dir=TEST_ROOT))
        self.path.chmod(0o700)
        self.marker = (self.path / MARKER).open('x+')
        fcntl.flock(self.marker, fcntl.LOCK_EX)
        self.marker.write(json.dumps({'pid':os.getpid(),'process_start':process_start(os.getpid()),'lock_protocol':1}))
        self.marker.flush()
        return self.path

    def __exit__(self,*exc):
        try:
            if self.path is not None:remove_run(self.path)
        finally:
            if self.marker is not None:
                self.marker.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',nargs='?',choices=('unit','quick','all','browser-server'),default='unit')
    parser.add_argument('--jobs',type=int,choices=range(1,9),default=2)
    args,extra=parser.parse_known_args()
    if args.mode == 'browser-server':
        if extra:parser.error('browser-server 不接受 pytest 参数')
        from tests.support import serve_browser
        def stop_server(*_):
            raise SystemExit(0)
        old = signal.signal(signal.SIGTERM, stop_server)
        try:
            with TestRun('browser') as path:
                serve_browser(path)
        finally:
            signal.signal(signal.SIGTERM, old)
        return 0
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','AVATAR_SYSTEM_ATR_ENABLED':'0','AVATAR_SYSTEM_AIP_ENABLED':'0','AVATAR_ENTITY_AIP_ENABLED':'0','WIT_REGISTRY_USERNAME':'','WIT_REGISTRY_PASSWORD':''}
    command=[sys.executable,'-m','pytest']
    if args.jobs>1 and '--collect-only' not in extra:command+=['-n',str(args.jobs),'--dist','worksteal']
    command+=extra or ['-q']
    process=None
    def interrupt(*_):raise KeyboardInterrupt
    old=signal.signal(signal.SIGTERM,interrupt)
    try:
        with TestRun('unit'):
            process=subprocess.Popen(command,env=env,start_new_session=True)
            return process.wait()
    except KeyboardInterrupt:return 130
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
        signal.signal(signal.SIGTERM,old);clean_stale_runs()


if __name__=='__main__':raise SystemExit(main())
