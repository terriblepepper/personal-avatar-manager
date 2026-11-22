"""Bounded Linux workers for plain-text reads. The parent transfers an authorized open file."""
import atexit
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from avatar.core.performance import measure

MAX_REPLY_BYTES = 6_000_256  # One million characters, including JSON control escapes.
MAX_TASKS = 64
MAX_POOLED_BYTES = 4096
TIMEOUT_SECONDS = 45
LIFETIME_SECONDS = 300
LINUX_FDS = sys.platform == 'linux' and Path('/proc/self/fd').is_dir()


def supports(extension):
    from avatar.files.parsing import PLAIN_TEXT_EXTENSIONS
    return LINUX_FDS and extension in PLAIN_TEXT_EXTENSIONS


@measure('material_read')
def read_original(path,extension,**options):
    if supports(extension):
        return parse(path, extension, 'read', **options)
    from avatar.core.config import ROOT
    result=subprocess.run([sys.executable,'-m','avatar.files.parsing.reader',str(path),extension],input=json.dumps(options).encode(),cwd=ROOT,capture_output=True,timeout=90 if extension in ('.doc','.docx') else 45,check=True)
    return json.loads(result.stdout)


class _Worker:
    def __init__(self):
        self.socket, child = socket.socketpair()
        self.socket.settimeout(TIMEOUT_SECONDS)
        try:
            with child:
                self.process = subprocess.Popen(
                    [sys.executable, '-IB', __file__, str(child.fileno())],
                    pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={'LANG': 'C.UTF-8'},
                )
        except BaseException:
            self.socket.close()
            raise
        self.reader = self.socket.makefile('rb')
        self.deadline = time.monotonic() + LIFETIME_SECONDS
        self.jobs = 0
        self.expiry = threading.Timer(LIFETIME_SECONDS, self.retire_idle)
        self.expiry.daemon = True
        try:
            self.expiry.start()
        except RuntimeError:
            self.close()
            raise

    def retire_idle(self):
        with _guard:
            if self not in _idle:
                return
            _idle.remove(self)
        self.close()

    def call(self, descriptor, request):
        socket.send_fds(self.socket, [b'F'], [descriptor])
        self.socket.sendall(request)
        raw = self.reader.readline(MAX_REPLY_BYTES)
        if not raw.endswith(b'\n'):
            raise OSError('文本解析进程中断或返回过大')
        self.jobs += 1
        return json.loads(raw)

    def close(self):
        self.expiry.cancel()
        self.reader.close()
        self.socket.close()
        self.process.kill()
        self.process.wait()


# A worker has no database handle, per-user cache, filesystem path in IPC, or model credentials.
_slots = threading.BoundedSemaphore(2)
_index_slot = threading.BoundedSemaphore(1)  # Keep a worker available for foreground original reads.
_idle = []
_guard = threading.Lock()
_generation = 0


def close():
    global _generation
    with _guard:
        workers = _idle[:]
        _idle.clear()
        _generation += 1
    for worker in workers:
        worker.close()


atexit.register(close)


@contextmanager
def _lease(operation, deadline):
    with ExitStack() as acquired:
        for slot in ((_index_slot, _slots) if operation == 'native' else (_slots,)):
            if not slot.acquire(timeout=max(0, deadline - time.monotonic())):
                raise TimeoutError('等待文本解析进程超时')
            acquired.callback(slot.release)
        worker = None
        try:
            with _guard:
                generation = _generation
                worker = _idle.pop() if _idle else None
            if worker and (worker.process.poll() is not None or time.monotonic() >= worker.deadline):
                worker.close()
                worker = None
            if worker is None:
                worker = _Worker()
            yield worker
            with _guard:
                if generation == _generation and worker.jobs < MAX_TASKS and time.monotonic() < worker.deadline:
                    _idle.append(worker)
                    worker = None
        finally:
            if worker:
                worker.close()


def parse(path, extension, operation, **options):
    if operation not in ('native', 'read') or not supports(extension):
        raise ValueError('不支持的文本解析操作')
    # O_NONBLOCK prevents a replaced FIFO from blocking before we can check its type.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        if operation == 'native':
            return {'text':'','status':'needs_description'}
        return {'error': '原件解析失败（' + type(error).__name__ + '）'}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('文本原件必须是普通文件')
        if info.st_size > MAX_POOLED_BYTES:
            # Large concurrent replies saturate the two slots; preserve one-shot parsing on the same inode.
            from avatar.core.config import ROOT
            module = 'avatar.files.parsing.reader' if operation == 'read' else 'avatar.files.parsing.extract'
            args = [sys.executable, '-B', '-m', module, f'/proc/self/fd/{descriptor}', extension]
            if operation == 'native':
                args.append('--native-only')
            result = subprocess.run(args, input=json.dumps(options).encode(), cwd=ROOT,
                                    capture_output=True, check=True, timeout=TIMEOUT_SECONDS,
                                    pass_fds=(descriptor,), env={'LANG':'C.UTF-8'})
            return json.loads(result.stdout)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        request = json.dumps([operation, extension, options, deadline], ensure_ascii=False).encode() + b'\n'
        if len(request) > 8192:
            raise ValueError('文本读取参数过大')
        with _lease(operation, deadline) as worker:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('文本解析等待超时')
            worker.socket.settimeout(remaining)
            return worker.call(descriptor, request)
    finally:
        os.close(descriptor)


def serve(sock):
    import resource
    import re
    from avatar.files.parsing.extract import native_plain
    from avatar.files.parsing.reader import read
    for kind, maximum in ((resource.RLIMIT_AS, 512 * 1024**2), (resource.RLIMIT_NOFILE, 64),
                          (resource.RLIMIT_CPU, 30 * MAX_TASKS)):
        inherited = resource.getrlimit(kind)
        limit = min([maximum, *(bound for bound in inherited if bound != resource.RLIM_INFINITY)])
        resource.setrlimit(kind, (limit, limit))
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    with sock.makefile('rb') as inbound:
        for _ in range(MAX_TASKS):
            marker, fds, flags, _ = socket.recv_fds(sock, 1, 1)
            if marker != b'F' or len(fds) != 1 or flags & socket.MSG_CTRUNC:
                return
            with os.fdopen(fds[0], 'rb') as original:
                signal.alarm(TIMEOUT_SECONDS)
                signal.setitimer(signal.ITIMER_PROF, 30)
                operation, extension, options, deadline = json.loads(inbound.readline(8193))
                signal.setitimer(signal.ITIMER_REAL, max(.001, deadline - time.monotonic()))
                if not supports(extension) or operation not in ('native', 'read'):
                    return
                path = Path(f'/proc/self/fd/{original.fileno()}')
                try:
                    value = native_plain(path) if operation == 'native' else read(path, extension, **options)
                except Exception as error:
                    value = {'text':'','status':'needs_description'} if operation == 'native' else {
                        'error': str(error) if isinstance(error, ValueError) else '原件解析失败（' + type(error).__name__ + '）'}
                sock.sendall(json.dumps(value, ensure_ascii=False).encode() + b'\n')
                if options.get('query'):
                    re.purge()  # The regex cache would retain a previous reader’s search phrase.
                del value, options
                signal.setitimer(signal.ITIMER_PROF, 0)
                signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    with socket.socket(fileno=int(sys.argv[1])) as channel:
        serve(channel)
