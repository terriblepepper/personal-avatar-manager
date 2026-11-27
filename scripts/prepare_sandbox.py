# 模块说明：仅由运维运行的本地沙箱依赖准备；不执行用户技能，不读取 .env，不依赖 Docker。
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from avatar.sandbox.sandbox import ANALYSIS_PACKAGES,RUNTIME,LocalPythonSandbox


def main():
    argparse.ArgumentParser(description='准备并自检本地 Python 沙箱的离线依赖').parse_args()
    RUNTIME.mkdir(parents=True,exist_ok=True)
    env={**os.environ,'DENO_DIR':str(RUNTIME/'cache')}
    subprocess.run([str(Path(sys.prefix)/'bin/deno'),'cache','--no-lock','jsr:@langchain/pyodide-sandbox@0.0.4'],env=env,check=True)
    package=RUNTIME/'cache/npm/registry.npmjs.org/pyodide/0.27.8'
    lock=json.loads((package/'pyodide-lock.json').read_text())
    # 按 Pyodide 锁文件准备传递依赖，运行时无 CDN/PyPI 权限。
    packages=set()
    def include(name):
        if name in packages:return
        packages.add(name)
        for dependency in lock['packages'][name]['depends']:include(dependency)

    for name in ('micropip',*ANALYSIS_PACKAGES.values()):include(name)
    source = os.getenv('AVATAR_PYODIDE_BASE_URL', 'https://cdn.jsdelivr.net/pyodide/v0.27.8/full').rstrip('/')
    with httpx.Client(timeout=60,follow_redirects=False) as client:
        for name in sorted(packages):
            item=lock['packages'][name];target=package/item['file_name']
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==item['sha256']:continue
            response=client.get(source+'/'+item['file_name']);response.raise_for_status()
            if hashlib.sha256(response.content).hexdigest()!=item['sha256']:raise RuntimeError('Pyodide 资源摘要不匹配：'+name)
            target.write_bytes(response.content)
    (RUNTIME/'ready').write_text('@langchain/pyodide-sandbox=0.0.4; pyodide=0.27.8\n')
    try:
        result=asyncio.run(LocalPythonSandbox().execute('setup',{'check.py':b"print('runtime-ready')"},'check.py',[],timeout=15))
        if result['status']!='success' or 'runtime-ready' not in str(result['stdout']):raise RuntimeError('沙箱自检失败：'+str(result))
    except BaseException:
        (RUNTIME/'ready').unlink(missing_ok=True)
        raise
    print('本地 Python 沙箱依赖及禁网执行自检通过。')


if __name__=='__main__':main()
