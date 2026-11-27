"""Build a shareable release from explicit inputs; never export a running instance."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(project, output, *formats):
    subprocess.run([sys.executable, '-m', 'build', '--no-isolation',
                    *formats, '--outdir', str(output), str(project)], check=True, cwd=ROOT)


def copy_release_dependencies(staging):
    packages = {p['name']: p for p in tomllib.loads((ROOT / 'uv.lock').read_text())['package']}
    wheels = []
    for name in ('redis', 'wit', 'acps-sdk'):
        wheel, = packages[name]['wheels']
        wheels.append(wheel)
    for abi in ('cp312-cp312', 'cp313-cp313'):
        wheel, = [w for w in packages['confluent-kafka']['wheels']
                  if w['url'].endswith(f'{abi}-manylinux_2_28_x86_64.whl')]
        wheels.append(wheel)
    for wheel in wheels:
        filename = wheel.get('filename') or wheel['url'].rsplit('/', 1)[1]
        source = ROOT / 'vendor' / 'wheels' / filename
        if 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest() != wheel['hash']:
            raise ValueError(f'{filename} 与 uv.lock 不符')
        shutil.copy2(source, staging / 'dependencies' / filename)
    for service, config in (('redis', 'redis.conf'), ('kafka', 'broker.env')):
        (staging / service).mkdir()
        for filename in ('service.sh', config):
            shutil.copy2(ROOT / service / filename, staging / service / filename)


def main():
    argparse.ArgumentParser(description='构建 wheel、源码包和含依赖的离线 ZIP').parse_args()
    frontend = ROOT / 'frontend'
    npm = shutil.which('npm')
    if not npm:
        raise SystemExit('构建 Release 需要 Node.js >=22.12 和 npm；运行包无需 Node.js')
    subprocess.run([npm, 'ci', '--offline', '--no-audit', '--no-fund'], cwd=frontend, check=True)
    subprocess.run([npm, 'run', 'build'], cwd=frontend, check=True)
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='avatar-release-') as directory:
        staging = Path(directory)
        # Default frontend flow rebuilds the wheel from the generated sdist.
        build(ROOT, staging)
        wheels = staging / 'dependencies'
        wheels.mkdir()
        copy_release_dependencies(staging)
        build(ROOT / 'vendor' / 'EverOS', wheels, '--wheel')
        shutil.copy2(ROOT / 'docs' / '打包与组件部署.md', staging / '安装说明.md')
        shutil.copy2(ROOT / '.env.example', staging / 'config.env.example')
        shutil.copy2(ROOT / 'scripts' / 'install_runtime.py', staging / 'install_runtime.py')
        files = sorted(path for path in staging.rglob('*') if path.is_file())
        checksums = '\n'.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.relative_to(staging).as_posix() for path in files)
        (staging / 'SHA256SUMS').write_text(checksums + '\n')
        archive = output / f'personal-avatar-manager-{version}.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staging.rglob('*')):
                if path.is_file():
                    bundle.write(path, path.relative_to(staging))
        for path in staging.glob('personal_avatar_manager-*'):
            shutil.copy2(path, output / path.name)
        print(f'可分发包：{archive}')


if __name__ == '__main__':
    main()
