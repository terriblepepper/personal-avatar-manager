# 模块说明：Pyodide/Deno 本地 Python 沙箱；不向技能授予宿主文件、网络、环境或进程权限。
import asyncio
import base64
import json
import importlib.util
import io
import zipfile
from contextlib import nullcontext
from functools import lru_cache
import os
from pathlib import Path, PurePosixPath
import signal
import sys
import tempfile
from avatar.core.execution import run_blocking

RUNTIME = Path(os.environ.get('AVATAR_SANDBOX_RUNTIME') or Path(sys.prefix) / 'share/avatar-sandbox').resolve()
ANALYSIS_PACKAGES = {'numpy': 'numpy', 'pandas': 'pandas', 'matplotlib': 'matplotlib', 'PIL': 'pillow'}


class SandboxUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('服务器的 Python 沙箱运行环境尚未就绪，请联系管理员准备运行环境后重试。')


@lru_cache(maxsize=1)
def workspace_policy():
    return Path(__file__).with_name('workspace_policy.py').read_text(encoding='utf-8')


# 职责：限制本进程总体及单账户并发；每次执行创建独立进程和 WASM 文件系统，不共享用户状态。
# 结果与约束：运行已缓存的 Deno 入口，限制输出并在取消时回收进程组，不反序列化 pickle。
class LocalPythonSandbox:
    # 职责：初始化全局并发信号量和按账户引用计数的排队信号量，不创建执行环境。
    def __init__(self):
        self.slots = asyncio.Semaphore(4)
        self.users = {}

    # 职责：验证虚拟文件名，资源仅通过内存拷贝进入 WASM，不挂载宿主目录。
    @staticmethod
    def virtual_path(name):
        if not isinstance(name,str):
            raise ValueError('沙箱资源路径无效')
        path = PurePosixPath(name)
        if (not path.parts or str(path) != name or path.parts[0] in ('_runtime','https')
                or path.is_absolute() or '..' in path.parts or '\\' in name or '\x00' in name):
            raise ValueError('沙箱资源路径无效')
        return str(path)

    # 职责：把服务端已安装的纯 Python PDF 库复制进虚拟目录；不挂载宿主目录或联网安装依赖。
    @staticmethod
    @lru_cache(maxsize=1)
    def pdf_archive():
        package = Path(importlib.util.find_spec('pypdf').origin).parent
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(package.rglob('*.py')):
                archive.write(path, 'pypdf/'+path.relative_to(package).as_posix())
        return base64.b64encode(buffer.getvalue()).decode('ascii')

    # 职责：执行调用方已授权的 Python 文件（技能脚本或任务代码），只接收字符串参数与有限资源，返回文字及 outputs 内文件。
    async def execute(self, user_id, resources, script, args, *, timeout=60, https_inputs=None, authorize=None, workspace=None):
        if script not in resources or not script.endswith('.py'):
            raise ValueError('只支持执行当前技能包内的 Python 脚本')
        if not isinstance(args,list) or len(args)>40 or any(not isinstance(a,str) or len(a)>2000 for a in args):
            raise ValueError('脚本参数必须为最多 40 个短字符串')
        if self.users.get(user_id,(None,0))[1]>=2 or sum(count for _,count in self.users.values())>=32:
            raise RuntimeError('沙箱任务排队已满，请等待现有任务完成后再试')
        # 空闲账户锁及时移除，避免长期运行累计账户对象；排队和执行均可取消。
        lock, count = self.users.get(user_id, (asyncio.Semaphore(1),0))
        self.users[user_id] = (lock,count+1)
        try:
            async with lock, self.slots:
                from avatar.skills.https_inputs import fetch_inputs
                if authorize:
                    await run_blocking(authorize)
                library = RUNTIME / 'cache/npm/registry.npmjs.org/pyodide/0.27.8'
                if (not (RUNTIME / 'ready').is_file() or not (library / 'pyodide.asm.wasm').is_file()
                        or not os.access(Path(sys.prefix) / 'bin/deno', os.X_OK)):
                    raise SandboxUnavailable()
                remote = await fetch_inputs(https_inputs or [])
                code = await run_blocking(self.prepare, resources, script, args, remote)
                if authorize:
                    await run_blocking(authorize)
                if workspace is not None:
                    return await self.run(code, timeout, workspace=workspace)
                return await self.run(code, timeout)
        finally:
            _,count=self.users[user_id]
            if count==1:del self.users[user_id]
            else:self.users[user_id]=(lock,count-1)

    def prepare(self, resources, script, args, remote):
        if len(resources)+len(remote)>512:
            raise ValueError('沙箱输入超过 512 个文件')
        payload = {}
        size = sum(map(len, remote.values()))
        for name, source in resources.items():
            name = self.virtual_path(name)
            body = source() if callable(source) else source
            size += len(body)
            if size > 8*1024*1024:
                raise ValueError('沙箱输入超过 8 MiB')
            payload[name] = base64.b64encode(body).decode('ascii')
        payload.update({name:base64.b64encode(body).decode('ascii') for name,body in remote.items()})
        payload['_runtime/pypdf.zip'] = self.pdf_archive()
        code = ('import os, sys, base64, runpy\nfrom pathlib import Path\n'
                'os.makedirs("/workspace/outputs", exist_ok=True)\n'
                f'for name, body in {payload!r}.items():\n'
                '    p=Path("/workspace", name); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(base64.b64decode(body))\n'
                'from pyodide.code import find_imports\n'
                'import tokenize\n'
                f'_analysis_packages={ANALYSIS_PACKAGES!r}\n'
                '_packages=set()\n'
                f'for _name in {[name for name in payload if name.endswith(".py")]!r}:\n'
                '    try:\n'
                '        with tokenize.open("/workspace/"+_name) as _script:\n'
                '            _packages.update(_analysis_packages[n] for n in find_imports(_script.read()) if n in _analysis_packages)\n'
                '    except (SyntaxError, UnicodeError, ValueError):\n'
                '        pass\n'
                'await globals().pop("_avatar_load_packages")(",".join(sorted(_packages)))\n'
                'os.environ.update(MPLBACKEND="Agg", MPLCONFIGDIR="/workspace/_runtime/matplotlib")\n'
                # Pyodide 的 Matplotlib 首次初始化在虚拟库路径生成字体缓存，须早于技能代码及工作区限制。
                'if "matplotlib" in _packages:\n    import matplotlib.font_manager\n'
                'os.chdir("/workspace")\n'
                + workspace_policy() + '\n_install_workspace_policy()\n'
                f'sys.argv={[script,*args]!r}\nsys.path.insert(0,"/workspace")\nsys.path.insert(0,"/workspace/_runtime/pypdf.zip")\n'
                + f'try:\n    runpy.run_path({script!r},run_name="__main__")\n'
                'except SystemExit as exc:\n    if exc.code not in (None,0): raise\n'
                # 只导出 outputs 中的普通文件；宿主还会独立复核文件名、大小和编码。
                'def _avatar_exports():\n'
                '    files=[]; total=0\n'
                '    for root, dirs, names in os.walk("/workspace/outputs", followlinks=False):\n'
                '        if len(dirs)+len(names)>512: raise ValueError("输出目录条目过多")\n'
                '        if any(os.path.islink(os.path.join(root,n)) for n in dirs+names): raise ValueError("不能导出链接")\n'
                '        for name in names:\n'
                '            path=os.path.join(root,name)\n'
                '            if not os.path.isfile(path): raise ValueError("输出必须为普通文件")\n'
                '            if os.stat(path).st_size>10*1024*1024: raise ValueError("输出文件超过容量限制")\n'
                '            body=Path(path).read_bytes(); total+=len(body)\n'
                '            if len(body)>10*1024*1024 or total>20*1024*1024: raise ValueError("输出文件超过容量限制")\n'
                '            files.append({"filename":name,"data":base64.b64encode(body).decode()})\n'
                '    return {"files":files}\n_avatar_exports()\n')
        return code

    # 职责：在临时目录运行离线沙箱命令并收集有界输出；所有结束路径杀死并回收所属进程组。
    async def run(self, code, timeout, *, workspace=None):
        cache=RUNTIME/'cache'
        directory = nullcontext(workspace) if workspace is not None else tempfile.TemporaryDirectory(prefix='avatar-python-')
        with directory as work:
            private_cache=Path(work)/'cache'
            private_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            npm = private_cache/'npm'
            if not npm.is_symlink():
                npm.symlink_to(cache/'npm',target_is_directory=True)
            library=cache/'npm/registry.npmjs.org/pyodide/0.27.8'
            source=Path(work)/'request.py'
            await run_blocking(source.write_text, code, encoding='utf-8')
            source.chmod(0o600)
            command = [
                '/usr/bin/prlimit', '--cpu=60', '--', str(Path(sys.prefix) / 'bin/deno'), 'run',
                '--no-prompt', '--cached-only', '--no-lock', '--deny-net', '--deny-env',
                '--deny-run', '--deny-ffi', '--deny-write', '--deny-import', f'--allow-read={library},{source}',
                '--node-modules-dir=none', '--v8-flags=--max-old-space-size=512',
                str(Path(__file__).with_name('runtime.ts')), str(source),
            ]
            process=await asyncio.create_subprocess_exec(*command,cwd=work,env={'DENO_DIR':str(private_cache),'PATH':str(Path(sys.prefix)/'bin')+':/usr/bin:/bin','HOME':str(work)},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
            # 职责：限制 stdout/stderr 管道，超出即终止而不是把无限输出读进 Web 服务内存。
            async def read(stream):
                result=bytearray()
                while block:=await stream.read(16384):
                    result.extend(block)
                    if len(result)>30*1024*1024:raise RuntimeError('沙箱输出超过 30 MiB，执行已中止')
                return result.decode(errors='replace')
            # Deno/V8 会预留大块虚拟地址，按实际常驻内存限制，不能用 RLIMIT_AS 误杀启动。
            async def memory_watch():
                while process.returncode is None:
                    try:
                        status=Path(f'/proc/{process.pid}/status').read_text()
                        rss=int(next(line.split()[1] for line in status.splitlines() if line.startswith('VmRSS:')))
                        if rss>512*1024:raise RuntimeError('沙箱内存超过 512 MiB，执行已中止')
                    except (FileNotFoundError,ProcessLookupError,StopIteration):pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.1)
                    except TimeoutError:
                        continue
            readers=[asyncio.create_task(read(process.stdout)),asyncio.create_task(read(process.stderr)),asyncio.create_task(memory_watch())]
            try:
                async with asyncio.timeout(timeout):
                    out,err,_=await asyncio.gather(*readers)
                    await process.wait()
                return await asyncio.to_thread(self.decode_result,out,err,process.returncode)
            except TimeoutError:raise RuntimeError(f'沙箱执行超过 {timeout} 秒，已终止') from None
            finally:
                if process.returncode is None:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                await process.wait()
                for task in readers:
                    if not task.done():task.cancel()
                await asyncio.gather(*readers,return_exceptions=True)

    @staticmethod
    def decode_result(out, err, exit_code):
        try:value=json.loads(out)
        except ValueError:raise RuntimeError('沙箱运行失败（退出码 '+str(exit_code)+'）：'+err[-2000:]) from None
        if not isinstance(value,dict) or not isinstance(value.get('success'),bool):
            raise RuntimeError('沙箱运行结果格式无效')
        if len(json.dumps([value.get('stdout',[]),value.get('stderr',[])],ensure_ascii=False).encode())>1024*1024:
            raise RuntimeError('沙箱文字输出超过 1 MiB，执行已中止')
        files=value.get('files',[]) if value['success'] else []
        if not isinstance(files,list):raise RuntimeError('沙箱生成文件清单无效')
        from avatar.files.conversation.resources import validate_output
        decoded=[]
        for item in files:
            if not isinstance(item,dict) or not isinstance(item.get('data'),str) or len(item['data'])>14*1024*1024:
                raise RuntimeError('沙箱生成文件格式无效')
            body=base64.b64decode(item['data'],validate=True)
            validate_output(item.get('filename'),body)
            decoded.append({'filename':item['filename'],'data':body})
        if sum(len(item['data']) for item in decoded)>20*1024*1024:raise RuntimeError('沙箱生成文件总量超限')
        return {'status':'success' if value.get('success') else 'error','stdout':value.get('stdout',[]),'stderr':value.get('stderr',[]),'error':value.get('error',''),'files':decoded}
