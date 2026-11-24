"""Copy maintained resources into the wheel without duplicating them in source."""
from pathlib import Path
from shutil import copy2, copytree, rmtree
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPlatform(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        resources = Path(self.build_lib) / 'avatar' / '_resources'
        frontend = root / 'frontend' / 'dist'
        ops = root / 'admin-frontend' / 'dist'
        if not self.editable_mode and any(not (site / 'index.html').is_file() for site in (frontend, ops)):
            raise RuntimeError('缺少 Vue 构建产物；请先 cd frontend && npm ci && npm run build')
        if resources.exists():
            rmtree(resources)  # Drop generated files from earlier builds before assembling the wheel.
        if frontend.is_dir():
            copytree(frontend, resources / 'frontend' / 'dist')
        if ops.is_dir():
            copytree(ops, resources / 'admin-frontend' / 'dist')
        for document in ('用户操作手册-purge.md', '操作手册.md', '知识库与soul.md', '资料RAG与嵌入.md'):
            target = resources / 'docs' / document
            target.parent.mkdir(parents=True, exist_ok=True)
            copy2(root / 'docs' / document, target)
        pictures = Path('docs') / 'picture' / '用户操作手册-purge'
        copytree(root / pictures, resources / pictures)
        target = resources / 'tools' / 'everos'
        target.mkdir(parents=True, exist_ok=True)
        copy2(root / 'tools' / 'everos' / 'worker.py', target / 'worker.py')


setup(cmdclass={'build_py': BuildPlatform})
