"""Execute the local converter with Internet sockets denied, Unix IPC retained."""
import ctypes
import ctypes.util
import errno
import os
import socket
import sys


def main():
    if sys.platform!='linux':
        raise RuntimeError('Word 转换的网络隔离目前需要 Linux/libseccomp')
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    library=ctypes.util.find_library('seccomp')
    if not library:raise RuntimeError('Word 转换需要 libseccomp2 网络隔离支持')
    seccomp=ctypes.CDLL(library)
    class Comparison(ctypes.Structure):
        _fields_=[('arg',ctypes.c_uint),('op',ctypes.c_int),('first',ctypes.c_uint64),('second',ctypes.c_uint64)]
    seccomp.seccomp_init.argtypes=[ctypes.c_uint32];seccomp.seccomp_init.restype=ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint,Comparison]
    seccomp.seccomp_load.argtypes=[ctypes.c_void_p]
    seccomp.seccomp_release.argtypes=[ctypes.c_void_p]
    context=seccomp.seccomp_init(0x7fff0000)
    if not context:raise RuntimeError('Word 转换网络隔离初始化失败')
    try:
        syscall=seccomp.seccomp_syscall_resolve_name(b'socket')
        if syscall<0:raise RuntimeError('Word 转换网络隔离不可用')
        if seccomp.seccomp_rule_add(context,0x00050000|errno.EPERM,syscall,1,Comparison(0,1,socket.AF_UNIX,0))<0:
            raise RuntimeError('Word 转换网络隔离规则安装失败')
        if seccomp.seccomp_load(context)<0:raise RuntimeError('Word 转换网络隔离启用失败')
    finally:seccomp.seccomp_release(context)
    os.execv(sys.argv[1],sys.argv[1:])


if __name__=='__main__':main()
