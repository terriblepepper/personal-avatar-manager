"""运维命令、服务生命周期、构建资源与模块边界。"""
import ast
import fcntl
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# 运维入口的离线帮助、隔离安装与发行版本一致性

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_cleanup_preserves_live_run_across_pid_namespaces(monkeypatch, tmp_path):
    from scripts import run_tests as workspace
    monkeypatch.setattr(workspace, 'TEST_ROOT', tmp_path / 'runs')
    with workspace.TestRun('active') as active:
        # Another namespace can observe a different process under the same PID.
        monkeypatch.setattr(workspace, 'process_start', lambda pid: 'different-namespace')
        workspace.clean_stale_runs()
        assert active.is_dir(), 'An active run must be protected independently of PID visibility'
    assert not active.exists()


@pytest.mark.parametrize(('mode', 'adapter'), [
    ('demo', 'DemoRegistrationProvider'),
    ('acps-direct', 'DirectRegistrationProvider'),
])
def test_environment_provider_controls_runtime_and_admin_mode(monkeypatch, tmp_path, mode, adapter, postgres_url):
    from fastapi.testclient import TestClient
    from avatar.api.app import create_app

    monkeypatch.setenv('DATABASE_URL', postgres_url)
    monkeypatch.setenv('AVATAR_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('AVATAR_PROVIDER', mode)
    app = create_app(run_worker=False)
    client = TestClient(app)
    try:
        assert type(app.state.service.provider).__name__ == adapter, 'Environment must select the active adapter'
        assert client.get('/api/health').json()['mode'] == mode, 'Health must report the configured mode'
        password = (tmp_path / 'initial-admin.txt').read_text().split('password: ')[1].strip()
        from tests.support import login
        login(client, 'admin', password)
        result = client.get('/api/admin/settings')
        assert result.status_code == 200, result.text
        environment = result.json()['environment']
        assert environment['registration_adapter'] == adapter
        assert environment['registration_mode'] == mode
        monkeypatch.setenv('AVATAR_PROVIDER', 'different-after-startup')
        assert client.get('/api/admin/settings').json()['environment'] == environment, 'Startup information must stay consistent until the next start'
    finally:
        client.close()
        app.state.chat.rag.store.close()
        app.state.database.close()


def test_runtime_environment_reads_package_versions_and_manual_resources(monkeypatch, tmp_path):
    from importlib import metadata
    from avatar.core import config

    manual = tmp_path / 'docs/操作手册.md'
    manual.parent.mkdir()
    manual.write_text('operator manual')
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    versions = {'wit': '8.7.6', 'acps-sdk': '5.4.3'}
    monkeypatch.setattr(metadata, 'version', lambda name: versions[name])
    settings = config.Settings(provider='acps-direct', oidc_public_url='https://platform.test',
                               system_aip_enabled=True, system_aip_public_url='https://system.test/aip/rpc',
                               entity_aip_enabled=True, entity_aip_port=12001, entity_aip_port_end=12010)
    environment = config.runtime_environment(settings, SimpleNamespace(mode='acps-direct'))
    assert environment == {
        'registration_adapter': 'SimpleNamespace', 'registration_mode': 'acps-direct',
        'communication': [{'name': 'Web', 'value': 'https://platform.test'},
                          {'name': '系统 mTLS AIP', 'value': 'https://system.test/aip/rpc'},
                          {'name': '个人分身 mTLS AIP', 'value': '12001-12010'}],
        'framework_versions': versions, 'manual_path': str(manual.resolve()),
    }, 'Every environment field must describe the supplied deployment'
    manual.unlink()
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, 'version', missing)
    environment = config.runtime_environment(config.Settings(), SimpleNamespace())
    assert environment['manual_path'] is None, 'Absent resources must not produce an invented path'
    assert environment['framework_versions'] == {'wit': None, 'acps-sdk': None}
    assert [item['value'] for item in environment['communication'][1:]] == [None, None]


def test_runtime_environment_uses_actual_injected_adapter_mode():
    from avatar.core.config import Settings, runtime_environment
    from avatar.protocols.registration.base import DemoRegistrationProvider

    environment = runtime_environment(Settings(provider='acps-direct'), DemoRegistrationProvider())
    assert environment['registration_adapter'] == 'DemoRegistrationProvider'
    assert environment['registration_mode'] == 'demo', 'An injected adapter must not be labelled with a different configured mode'


@pytest.mark.parametrize('name', ['build_release', 'prepare_sandbox'])
def test_preparation_help_exits_before_any_subprocess(name, monkeypatch, tmp_path):
    module = importlib.import_module('scripts.' + name)
    monkeypatch.setattr('sys.argv', [name, '--help'])
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    if hasattr(module, 'RUNTIME'):
        monkeypatch.setattr(module, 'RUNTIME', tmp_path / 'runtime')
    def forbidden(*args, **kwargs):
        pytest.fail('帮助命令不得构建、下载或执行外部程序')
    monkeypatch.setattr(module.subprocess, 'run', forbidden)
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 0, '帮助命令应正常退出'
    assert not list(tmp_path.iterdir()), '帮助命令不得写运行目录'


def test_office_uses_existing_system_runtime_without_downloading(monkeypatch):
    from scripts import install_runtime
    calls = []
    monkeypatch.setattr(install_runtime.shutil, 'which', lambda name: '/usr/bin/libreoffice')
    monkeypatch.setattr(install_runtime.subprocess, 'run', lambda command, **options: calls.append(command))
    install_runtime.install_office()
    assert calls == [['/usr/bin/libreoffice', '--headless', '--version']]


def test_ocr_extracts_packages_inside_current_environment(monkeypatch, tmp_path):
    from scripts import install_runtime
    calls = []
    monkeypatch.setattr('sys.prefix', str(tmp_path))
    monkeypatch.setattr(install_runtime.shutil, 'which', lambda name: '/usr/bin/' + name)
    def run(command, **options):
        calls.append((command, options))
        if command[0] == 'apt-get':
            Path(options['cwd'], 'ocr.deb').touch()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(install_runtime.subprocess, 'run', run)
    install_runtime.install_ocr()
    target = tmp_path / 'share/avatar-ocr'
    extraction = next(command for command, _ in calls if command[0] == 'dpkg-deb')
    assert extraction[-1] == str(target), 'deb 只解压到当前环境'
    command, options = calls[-1]
    assert command == [str(target / 'usr/bin/tesseract'), '--list-langs']
    assert options['env']['TESSDATA_PREFIX'] == str(target / 'usr/share/tesseract-ocr/5/tessdata')


def test_package_lock_and_runtime_versions_match(monkeypatch):
    from avatar.core.config import application_version

    monkeypatch.delenv('AVATAR_VERSION', raising=False)
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    packages = tomllib.loads((ROOT / 'uv.lock').read_text())['package']
    locked = next(package['version'] for package in packages if package['name'] == 'personal-avatar-manager')
    assert application_version() == project == locked


def test_environment_version_applies_to_platform_but_preserves_user_acs(monkeypatch, tmp_path, postgres_url):
    from avatar.api.app import create_app
    from avatar.core.config import Settings
    from avatar.protocols.identity.system import build_system_acs
    from avatar.protocols.identity.ontology import build_avatar_ontology_acs
    from avatar.protocols.identity.acs import AcsInput, build_acs

    monkeypatch.setenv('AVATAR_VERSION', ' 0.1.3+deployment.2 ')
    app = create_app(Settings(data_dir=tmp_path, database_url=postgres_url), run_worker=False)
    try:
        assert app.version == '0.1.3+deployment.2'
        assert build_system_acs()['version'] == app.version
        assert build_avatar_ontology_acs()['version'] == '1.0.0'
        personal = build_acs(AcsInput(name='用户的分身', version='2.5.7'), 'owner', 'https://example.com')
        assert personal['version'] == '2.5.7', '个人分身 ACS 使用用户填写的版本'
    finally:
        app.state.chat.rag.store.close()
        app.state.database.close()


@pytest.mark.parametrize('configured', ['', '  '])
def test_empty_version_uses_source_metadata_when_not_installed(monkeypatch, tmp_path, configured):
    from importlib import metadata
    from avatar.core import config

    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "0.1.3"\n')
    monkeypatch.setenv('AVATAR_VERSION', configured)
    monkeypatch.setattr(config, 'SOURCE_ROOT', tmp_path)
    def not_installed(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, 'version', not_installed)
    assert config.application_version() == '0.1.3'


# Run service scripts against private files and owned processes, never a real service

@pytest.fixture
def service_workspace(tmp_path):
    root = tmp_path / 'platform & | path'
    root.mkdir()
    for name in ['run.sh', 'service.sh']:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, root / name)
    (root / 'deploy').mkdir()
    shutil.copy2(ROOT / 'deploy/personal-avatar.service.in', root / 'deploy/personal-avatar.service.in')
    (root / '.venv/bin').mkdir(parents=True)
    (root / '.venv/bin/python').symlink_to(sys.executable)
    (root / 'data').mkdir()
    (root / '.env').write_text('AVATAR_PORT=18443\n')
    (root / 'uvicorn.py').write_text("import signal\nfrom pathlib import Path\nPath('data/uvicorn.ready').touch()\nsignal.pause()\n")
    commands = root / 'commands.jsonl'
    binaries = root / 'bin'
    binaries.mkdir()
    for name in ['systemctl', 'curl', 'uv']:
        executable = binaries / name
        executable.write_text(f'#!{sys.executable}\n' +
            'import json,os,sys\nfrom pathlib import Path\n' +
            'with open(os.environ["AVATAR_TEST_COMMANDS"],"a") as out:\n' +
            ' out.write(json.dumps([Path(sys.argv[0]).name,*sys.argv[1:]])+"\\n")\n')
        executable.chmod(0o755)
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'XDG_CONFIG_HOME': str(root / 'config'), 'AVATAR_TEST_COMMANDS': str(commands)}
    def call(*args):
        return subprocess.run(['bash', str(root / 'service.sh'), *args], cwd=root,
                              env=env, capture_output=True, text=True, timeout=10)
    def calls():
        return [json.loads(line) for line in commands.read_text().splitlines()] if commands.exists() else []
    return SimpleNamespace(root=root, env=env, call=call, calls=calls)


def test_service_help_and_invalid_command_have_no_side_effects(service_workspace):
    workspace = service_workspace
    marker = workspace.root / 'data/env-loaded'
    (workspace.root / '.env').write_text(f'touch "{marker}"\n')
    result = workspace.call('--help')
    assert result.returncode == 0, result.stderr
    assert all(command in result.stdout for command in ['start', 'stop', 'install'])
    assert workspace.call('invalid').returncode != 0, 'Unknown commands must fail'
    assert workspace.call('start', 'unexpected').returncode != 0, 'Unexpected arguments must fail'
    assert not marker.exists(), 'Help and invalid arguments must not source private configuration'
    assert not workspace.calls(), 'Help must not install dependencies or operate services'


def test_current_application_process_is_reused_and_stopped(service_workspace):
    workspace = service_workspace
    process = subprocess.Popen([str(workspace.root / '.venv/bin/python'), '-m', 'uvicorn',
                                'avatar.api.app:create_app'], cwd=workspace.root)
    pid_file = workspace.root / 'data/avatar.pid'
    pid_file.write_text(str(process.pid) + '\n')
    try:
        result = workspace.call('start')
        assert result.returncode == 0, result.stderr
        assert int(pid_file.read_text()) == process.pid, 'Starting twice must reuse the owned process'
        assert process.poll() is None, 'Start must leave the existing process alive'
        result = workspace.call('stop')
        assert result.returncode == 0, result.stderr
        assert process.wait(timeout=3) == -15, 'Stop must terminate the current app entry point'
        assert not pid_file.exists(), 'Successful stop must remove the stale PID file'
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=3)


@pytest.mark.parametrize('process_kind', ['unrelated', 'argument-bait', 'other-project'])
def test_stop_refuses_an_unrelated_or_other_project_process(service_workspace, tmp_path, process_kind):
    workspace = service_workspace
    other_project = process_kind == 'other-project'
    command = [str(workspace.root / '.venv/bin/python')]
    command += ['-m', 'uvicorn', 'avatar.api.app:create_app'] if other_project else ['-c', 'import signal;signal.pause()']
    if process_kind == 'argument-bait':
        command.append(' -m uvicorn avatar.api.app:create_app ')
    if other_project:
        (tmp_path / 'uvicorn.py').write_text('import signal\nsignal.pause()\n')
    process = subprocess.Popen(command, cwd=tmp_path if other_project else workspace.root)
    (workspace.root / 'data/avatar.pid').write_text(str(process.pid) + '\n')
    try:
        assert workspace.call('stop').returncode != 0, 'A PID alone does not prove process ownership'
        assert process.poll() is None, 'Unrelated processes must remain alive'
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_stop_succeeds_if_another_stop_removed_pid_while_waiting_for_lock(service_workspace):
    workspace = service_workspace
    pid_file = workspace.root / 'data/avatar.pid'
    pid_file.write_text('99999999\n')
    ready = workspace.root / 'data/lock-waiting'
    flock = workspace.root / 'bin/flock'
    flock.write_text('#!/usr/bin/env bash\n: > "$AVATAR_TEST_LOCK_READY"\nexec /usr/bin/flock "$@"\n')
    flock.chmod(0o755)
    with (workspace.root / 'data/process.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(['bash', str(workspace.root / 'service.sh'), 'stop'],
                                   cwd=workspace.root,
                                   env={**workspace.env, 'AVATAR_TEST_LOCK_READY': str(ready)},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), 'The second stop must reach the locked critical section'
            pid_file.unlink()
            fcntl.flock(lock, fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=3)
            assert process.returncode == 0, stdout + stderr
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=3)


@pytest.mark.parametrize('pid', ['', 'not-a-pid', '-1', '0'])
def test_stop_rejects_invalid_pid_files(service_workspace, pid):
    workspace = service_workspace
    (workspace.root / 'data/avatar.pid').write_text(pid + '\n')
    assert workspace.call('stop').returncode != 0, 'Invalid PIDs must never reach kill'
    assert not workspace.calls(), 'PID validation must stay local'


def test_service_install_preserves_path_and_starts_systemd_once(service_workspace):
    workspace = service_workspace
    result = workspace.call('install')
    assert result.returncode == 0, result.stderr
    unit = workspace.root / 'config/systemd/user/personal-avatar.service'
    text = unit.read_text()
    assert f'WorkingDirectory={workspace.root}\n' in text, 'Template substitution must preserve & and |'
    assert f'ExecStart="{workspace.root}/run.sh"\n' in text, 'The foreground entry point must remain stable'
    assert workspace.calls() == [
        ['systemctl', '--user', 'daemon-reload'],
        ['systemctl', '--user', 'enable', 'personal-avatar.service'],
        ['systemctl', '--user', 'start', 'personal-avatar.service'],
        ['curl', '-fsS', '--max-time', '2', 'http://127.0.0.1:18443/api/health'],
    ], 'Install must enable, start once, then check health using .env'
    assert workspace.call('stop').returncode == 0
    assert workspace.calls()[-1] == ['systemctl', '--user', 'stop', 'personal-avatar.service']


def test_postgres_lifecycle_follows_service_start_and_stop(service_workspace):
    workspace = service_workspace
    pg_home = workspace.root / 'pg-home'
    binaries = pg_home / 'usr/lib/postgresql/16/bin'
    binaries.mkdir(parents=True)
    executable = binaries / 'pg_ctl'
    executable.write_text(f'#!{sys.executable}\n' +
        'import json,os,sys\nfrom pathlib import Path\n' +
        'args=sys.argv[1:]\n' +
        'with open(os.environ["AVATAR_TEST_COMMANDS"],"a") as out:\n' +
        ' out.write(json.dumps(["pg_ctl",*args])+"\\n")\n' +
        'state=Path(os.environ["AVATAR_TEST_PG_STATE"])\n' +
        'if "status" in args:\n' +
        ' sys.exit(0 if state.exists() else 3)\n' +
        'if "start" in args:\n' +
        ' state.write_text("up")\n' +
        'if "stop" in args:\n' +
        ' state.unlink(missing_ok=True)\n')
    executable.chmod(0o755)
    (workspace.root / 'data/pgsql').mkdir()
    workspace.env['AVATAR_PG_HOME'] = str(pg_home)
    workspace.env['AVATAR_TEST_PG_STATE'] = str(workspace.root / 'data/pg-state')
    data = workspace.root / 'data'
    try:
        assert workspace.call('start').returncode == 0
        # The fake HTTP check succeeds before run.sh necessarily execs the server.
        ready = data / 'uvicorn.ready'
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), 'The fake application must finish exec before it can be stopped'
        assert workspace.call('stop').returncode == 0
        assert workspace.calls() == [
            ['pg_ctl', '-D', str(data / 'pgsql'), 'status'],
            ['pg_ctl', '-D', str(data / 'pgsql'), '-l', str(data / 'pgsql.startup.log'), '-w', 'start'],
            ['curl', '-fsS', '--max-time', '2', 'http://127.0.0.1:18443/api/health'],
            ['pg_ctl', '-D', str(data / 'pgsql'), 'status'],
            ['pg_ctl', '-D', str(data / 'pgsql'), '-m', 'fast', 'stop'],
        ], 'The database must start before the app and stop after it'
        assert not (data / 'pg-state').exists(), 'Stop must shut the database down'
    finally:
        pid_file = data / 'avatar.pid'
        if pid_file.exists():
            subprocess.run(['kill', '-TERM', pid_file.read_text().strip()], check=False)


# Exercise the optional project-owned Redis independently of the application

def test_optional_cache_with_no_installed_server(tmp_path):
    cache = tmp_path / 'redis'
    shutil.copytree(ROOT / 'redis', cache, ignore=shutil.ignore_patterns('*.sock', '*.lock', '*.pid', '*.log'))
    commands = tmp_path / 'commands'
    commands.mkdir()
    for name in ('bash', 'dirname', 'readlink', 'flock'):
        (commands / name).symlink_to(shutil.which(name))
    env = {'PATH': str(commands), 'AVATAR_REDIS_SOCKET': ''}
    command = [str(cache / 'service.sh'), 'start']
    disabled = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert disabled.returncode == 0 and not disabled.stderr
    env['AVATAR_REDIS_SOCKET'] = 'redis/redis.sock'
    degraded = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert degraded.returncode == 0 and 'Redis' in degraded.stderr
    assert not (cache / 'redis.sock').exists()

def test_release_contains_locked_cache_client_and_only_public_files(tmp_path):
    import tomllib
    import hashlib
    from scripts.build_release import copy_redis

    (tmp_path / 'dependencies').mkdir()
    copy_redis(tmp_path)
    assert {f.name for f in (tmp_path / 'redis').iterdir()} == {'redis.conf', 'service.sh'}
    assert os.access(tmp_path / 'redis' / 'service.sh', os.X_OK)
    package = next(p for p in tomllib.loads((ROOT / 'uv.lock').read_text())['package'] if p['name'] == 'redis')
    wheel, = (tmp_path / 'dependencies').glob('redis-*.whl')
    assert f'redis-{package["version"]}-' in wheel.name
    assert 'sha256:' + hashlib.sha256(wheel.read_bytes()).hexdigest() == package['wheels'][0]['hash']


# frontend serving

def test_database_is_closed_when_runtime_start_fails(context, monkeypatch):
    app, _, _ = context
    closed = []
    async def failed_start():
        raise RuntimeError('startup failed')
    monkeypatch.setattr(app.state.runtime, 'start', failed_start)
    monkeypatch.setattr(app.state.database, 'close', lambda: closed.append(True))
    with pytest.raises(RuntimeError, match='startup failed'):
        with TestClient(app):
            pass
    assert closed == [True]


def test_spa_refresh_keeps_api_static_and_unknown_paths_distinct(tmp_path):
    from avatar.api.frontend import mount_frontend
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<div id="app">Vue</div>')
    (dist / 'assets' / 'app-123.js').write_text('export const app = 1')
    (dist / 'static').mkdir()
    (dist / 'static' / 'system-avatar.svg').write_text('<svg/>')
    app = FastAPI()

    @app.get('/api/me')
    def me():
        return {'user': 'current'}

    mount_frontend(app, dist)
    client = TestClient(app)
    assert 'Vue' in client.get('/chat').text
    assert 'Vue' in client.get('/admin/reviews').text
    assert 'Vue' in client.get('/admin/custom-section').text
    assert client.get('/api/me').json() == {'user': 'current'}
    for path in ('/api/missing', '/assets/missing.js', '/static/core/app.js', '/static/index.html',
                 '/admin/missing.css', '/admin/nested/section', '/missing.css', '/overview', '/unknown'):
        assert client.get(path).status_code == 404, path
    assert client.get('/static/system-avatar.svg').headers['content-type'] == 'image/svg+xml'
    assert client.get('/assets/app-123.js').headers['content-type'].startswith('text/javascript')
    assert 'immutable' in client.get('/assets/app-123.js').headers['cache-control']
    assert client.get('/chat').headers['cache-control'] == 'no-cache'


def test_source_checkout_without_build_keeps_api_available(tmp_path):
    from avatar.api.frontend import mount_frontend
    app = FastAPI()

    @app.get('/api/health')
    def health():
        return {'ok': True}

    mount_frontend(app, tmp_path / 'dist')
    client = TestClient(app)
    assert client.get('/api/health').json() == {'ok': True}
    response = client.get('/')
    assert response.status_code == 503
    assert 'npm run build' in response.json()['detail']
    assert client.get('/static/core/app.js').status_code == 404


# 模块边界和隔离解析入口

def internal_dependencies(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith('avatar.'):
                yield node.module
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names if alias.name.startswith('avatar.'))


def test_application_packages_follow_one_way_dependencies():
    edges = set()
    violations = []
    root = ROOT / 'avatar'
    for path in root.rglob('*.py'):
        package = path.relative_to(root).parts[0]
        for dependency in internal_dependencies(path):
            target = dependency.split('.')[1]
            if target == package:
                continue
            edges.add((package, target))
            if (package == 'core' or package == 'models' and target not in ('core', 'contracts')
                    or package == 'files' and target in ('conversation', 'rag', 'knowledge')
                    or package == 'rag' and target == 'conversation'):
                violations.append(f'{path.relative_to(ROOT)} -> {dependency}')
    cycles = sorted((left, right) for left, right in edges if left < right and (right, left) in edges)
    assert not violations, '\n'.join(violations)
    assert not cycles, cycles


def test_all_internal_imports_and_subprocess_modules_resolve():
    missing = []
    for path in (ROOT / 'avatar').rglob('*.py'):
        dependencies = set(internal_dependencies(path))
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.List, ast.Tuple)):
                for previous, target in zip(node.elts, node.elts[1:]):
                    if isinstance(previous, ast.Constant) and previous.value == '-m':
                        if isinstance(target, ast.Constant) and isinstance(target.value, str) and target.value.startswith('avatar.'):
                            dependencies.add(target.value)
        for dependency in dependencies:
            if importlib.util.find_spec(dependency) is None:
                missing.append(f'{path.relative_to(ROOT)} -> {dependency}')
    assert not missing, '\n'.join(missing)


def test_original_reader_runs_as_an_isolated_module(tmp_path):
    original = tmp_path / 'sample.txt'
    original.write_text('授权原件正文\nsecond line')
    result = subprocess.run(
        [sys.executable, '-m', 'avatar.files.parsing.reader', str(original), '.txt'],
        input='{}', text=True, capture_output=True, check=True, cwd=ROOT, timeout=15,
    )
    parsed = json.loads(result.stdout)
    assert parsed['pages'][0]['text'] == original.read_text(), parsed
    assert parsed['complete'], parsed


def test_word_pdf_is_read_by_the_isolated_reader_and_upload_rejects_macros(tmp_path):
    from tests.support import word_document
    from avatar.files.word_pdfs import prepare_word_pdf
    import zipfile

    original = tmp_path / 'sample.docx'
    original.write_bytes(word_document('Word 原件正文'))
    pdf, _ = prepare_word_pdf(original, '.docx')
    result = subprocess.run(
        [sys.executable, '-m', 'avatar.files.parsing.reader', str(pdf), '.pdf'],
        input='{}', text=True, capture_output=True, check=True, cwd=ROOT, timeout=30,
    )
    assert 'Word原件正文' in json.loads(result.stdout)['pages'][0]['text'].replace(' ', ''), result.stdout

    rejected = tmp_path / 'macro.docx'
    rejected.write_bytes(original.read_bytes())
    with zipfile.ZipFile(rejected, 'a') as archive:
        archive.writestr('word/vbaProject.bin', b'macro')
    with pytest.raises(ValueError, match='DOCX 不接受宏'):
        prepare_word_pdf(rejected, '.docx')


def test_generation_and_rag_share_the_execution_authorization_context():
    from avatar.core.execution import authorization, current_generation
    from avatar.conversation.jobs.pipeline import generation_scope

    scope = {'materials': ['authorized-file'], 'files': [], 'skills': {}}
    assert current_generation.get() is None
    with generation_scope({'authorization': json.dumps(scope)}):
        assert authorization() == scope
    assert authorization() is None
