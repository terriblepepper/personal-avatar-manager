"""Serve the built Vue app without turning API or asset errors into HTML."""
from pathlib import Path
import re
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PAGES = {'', 'overview', 'avatar', 'profile', 'files', 'knowledge', 'models', 'skills',
         'dialogues', 'chat', 'messages', 'history', 'account', 'platform-entry',
         'admin'}


class BuildAssets(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response


def mount_frontend(app, dist: Path):
    if (dist / 'static').is_dir():
        app.mount('/static', StaticFiles(directory=dist / 'static'), name='static')
    if (dist / 'assets').is_dir():
        app.mount('/assets', BuildAssets(directory=dist / 'assets'), name='frontend-assets')

    @app.get('/{path:path}', include_in_schema=False)
    def frontend(path: str):
        page = path.rstrip('/')
        # The admin console renders unknown sections client-side, so /admin/:page must serve the SPA shell.
        if page not in PAGES and not re.fullmatch(r'admin/[\w-]+', page):
            raise HTTPException(404, '页面不存在')
        index = dist / 'index.html'
        if not index.is_file():
            raise HTTPException(503, '缺少 Vue 构建产物，请在 frontend 目录执行 npm ci 和 npm run build')
        return FileResponse(index, headers={
            'Cache-Control': 'no-cache', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; font-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        })
