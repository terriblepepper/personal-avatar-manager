"""Install private Office or OCR tools in the current Python environment."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def relocate(target):
    shutil.copytree(target/'usr/lib/libreoffice/share/.registry',target/'etc/libreoffice/registry',dirs_exist_ok=True)
    for path in target.rglob('*'):
        if path.is_symlink():
            link=os.readlink(path)
            if link.startswith('/') and (target/link.lstrip('/')).exists():
                path.unlink()
                path.symlink_to(target/link.lstrip('/'))
    program=target/'usr/lib/libreoffice/program'
    for path in program.glob('*rc'):
        if path.is_file():
            text=path.read_text()
            path.write_text(text.replace('file:///usr/',(target/'usr').as_uri()+'/').replace('file:///etc/',(target/'etc').as_uri()+'/'))


def install_office():
    existing=shutil.which('libreoffice') or shutil.which('soffice')
    if existing:
        subprocess.run([existing,'--headless','--version'],check=True,timeout=30)
        print('使用已安装的系统 LibreOffice。')
        return
    target=Path(os.environ.get('AVATAR_OFFICE_RUNTIME') or Path(sys.prefix)/'share/avatar-office').resolve()
    program=target/'usr/lib/libreoffice/program'
    env={**os.environ,'LD_LIBRARY_PATH':os.pathsep.join([str(program),*map(str,(target/'usr/lib').glob('*-linux-gnu'))])}
    if (program/'soffice').is_file():
        relocate(target)
        if subprocess.run([str(program/'soffice'),'--headless','--version'],env=env,timeout=30).returncode==0:
            print('当前虚拟环境 LibreOffice 已就绪。')
            return
    plan=subprocess.run(['apt-get','--simulate','install','--no-install-recommends','libreoffice-writer'],capture_output=True,text=True,check=True)
    packages=re.findall(r'^Inst (\S+)(?: \[[^]]+\])? \((\S+)',plan.stdout,re.M)
    target.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='avatar-office-install-') as directory:
        subprocess.run(['apt-get','-o','Acquire::Retries=3','download',*[name+'='+version for name,version in packages]],cwd=directory,check=True,timeout=600)
        for package in Path(directory).glob('*.deb'):
            subprocess.run(['dpkg-deb','-x',str(package),str(target)],check=True,timeout=60)
    relocate(target)
    env['LD_LIBRARY_PATH']=os.pathsep.join([str(program),*map(str,(target/'usr/lib').glob('*-linux-gnu'))])
    subprocess.run([str(program/'soffice'),'--headless','--version'],env=env,check=True,timeout=30)
    print('LibreOffice installed in',target)


def install_ocr():
    if not shutil.which('apt-get') or not shutil.which('dpkg-deb'):
        raise SystemExit('此安装器适用于 Debian/Ubuntu；其他系统请安装 Tesseract 及 eng、chi_sim 模型。')
    target=Path(sys.prefix)/'share/avatar-ocr';target.mkdir(parents=True,exist_ok=True)
    packages=('tesseract-ocr','libtesseract5','liblept5','tesseract-ocr-eng','tesseract-ocr-chi-sim')
    with tempfile.TemporaryDirectory(prefix='avatar-ocr-') as work:
        subprocess.run(['apt-get','download',*packages],cwd=work,check=True,timeout=180)
        for package in Path(work).glob('*.deb'):
            subprocess.run(['dpkg-deb','-x',str(package),str(target)],check=True,timeout=30)
    libraries=[str(path) for path in (target/'usr/lib').glob('*-linux-gnu')]
    env={**os.environ,'LD_LIBRARY_PATH':os.pathsep.join(libraries),'TESSDATA_PREFIX':str(target/'usr/share/tesseract-ocr/5/tessdata'),'OMP_THREAD_LIMIT':'1'}
    subprocess.run([str(target/'usr/bin/tesseract'),'--list-langs'],env=env,check=True,timeout=10)
    print('OCR 已安装到',target,'；运行时离线识别，不使用模型 Key。')


def main():
    parser=argparse.ArgumentParser(description='在当前 Python 环境安装 Office 或 OCR 运行文件（Debian/Ubuntu，无需 sudo）')
    parser.add_argument('runtime',choices=('office','ocr'))
    args=parser.parse_args()
    if args.runtime=='office':install_office()
    else:install_ocr()


if __name__=='__main__':main()
