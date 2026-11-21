"""在 WASM 解释器内限制普通 Python 文件操作。宿主隔离由独立 Deno 进程执行。"""
import os
import sys


def _install_workspace_policy():
    root = '/workspace'
    library = os.path.join(os.path.realpath(sys.base_prefix), 'lib') + '/'
    resolve = os.path.realpath
    filesystem = {'os.remove': 1, 'os.rmdir': 1, 'os.mkdir': 1, 'os.chdir': 1, 'os.chmod': 1,
                  'os.rename': 2, 'os.truncate': 1, 'os.utime': 1, 'os.chown': 1}
    def within(path, write):
        if not isinstance(path, (str, bytes, os.PathLike)):
            raise PermissionError('仅允许工作区文件路径')
        path = resolve(os.fsdecode(path))
        if path == root or path.startswith(root + '/'):
            return
        if not write and path.startswith(library):
            return
        raise PermissionError('文件路径超出沙箱工作区')

    def check(event, args):
        if event == 'open':
            path, _, flags = args
            within(path, flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        elif event in filesystem:
            for path in args[:filesystem[event]]:
                within(path, True)
            if event in ('os.rename', 'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown', 'os.utime'):
                if any(fd not in (-1, None) for fd in (args[-2:] if event == 'os.rename' else args[-1:])):
                    raise PermissionError('沙箱不支持相对目录描述符')
        elif event in ('os.listdir', 'os.scandir'):
            within(args[0] or '.', False)
        elif event in ('os.link', 'os.symlink', 'os.system', 'os.exec', 'os.posix_spawn', 'os.fork',
                       'subprocess.Popen', 'socket.connect', 'socket.bind'):
            raise PermissionError('沙箱不支持此系统操作')

    sys.addaudithook(check)
