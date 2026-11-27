"""Renew the existing platform identity. Persist the attempt before contacting the CA."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from avatar.core.execution import run_blocking
from avatar.core.db import dump, event, now
from avatar.protocols.identity.atr_config import read_atr_config
from avatar.protocols.identity.document import LocalEntityDirectory, identity_json
from avatar.protocols.identity.system import SYSTEM_ENTITY_ID, system_identity_paths
from avatar.protocols.registration.entity import certificate_projection
from avatar.protocols.registration.maintenance import RENEWAL_SCAN_SECONDS, maintain_registered

log = logging.getLogger(__name__)


class PlatformCertificate:
    def __init__(self, database, settings):
        self.database = database
        self.settings = settings
        self.wake = asyncio.Event()

    def snapshot(self):
        paths = system_identity_paths(self.settings.data_dir)
        if not paths.registration.is_file() or not paths.acs.is_file():
            return None, None
        document = identity_json(paths.acs)
        if not document.get('active'):
            return None, None
        aic = document.get('aic')
        config = read_atr_config(paths.registration, self.settings.registry_username, self.settings.registry_password)
        if (not aic or document.get('entityUserId') != SYSTEM_ENTITY_ID or config.mode != 'integrated'
                or self.settings.system_aip_aic and self.settings.system_aip_aic != aic):
            raise ValueError('平台注册身份不匹配')
        identity = LocalEntityDirectory.load(config, aic)
        projection = certificate_projection(identity.material, identity.generation, '')
        return config, {'aic': aic, **{k: projection[k] for k in ('id','issuer','expires_at','generation')}}

    @staticmethod
    def job(db):
        row = db.execute("SELECT value FROM settings WHERE key='platform_certificate_renewal'").fetchone()
        return json.loads(row['value']) if row else {}

    @staticmethod
    def save(db, job):
        db.execute("""INSERT INTO settings(key,value) VALUES('platform_certificate_renewal',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (dump(job),))

    def status(self):
        try:
            _, certificate = self.snapshot()
        except (ValueError, OSError, KeyError):
            certificate = None
        with self.database.connect() as db:
            return {'certificate': certificate, 'renewal': self.job(db)}

    def queue(self, current, actor, *, automatic=False):
        with self.database.connect(write=True) as db:
            last = self.job(db)
            if last.get('state') in ('queued','running') or automatic and last.get('id') == current['id']:
                return
            self.save(db, {'id':current['id'], 'state':'queued', 'updated_at':now(), 'actor':actor})
            event(db, None, actor, '平台证书续签已排队')

    async def request(self, fingerprint, actor):
        status = await run_blocking(self.status)
        current = status['certificate']
        if not current or current['id'] != fingerprint:
            raise HTTPException(409, '平台证书状态已变化或不可用，请刷新')
        try:
            await run_blocking(self.queue, current, actor)
        finally:
            self.wake.set()

    def claim(self):
        with self.database.connect(write=True) as db:
            job = self.job(db)
            if job.get('state') != 'queued':
                return None
            job.update(state='running', updated_at=now())
            self.save(db, job)
            return job

    def finish(self, job, state):
        with self.database.connect(write=True) as db:
            self.save(db, {**job, 'state':state, 'updated_at':now()})
            event(db, None, job['actor'], '平台证书续签结果', state)

    async def check(self):
        config, current = await run_blocking(self.snapshot)
        if current and datetime.fromisoformat(current['expires_at']) <= datetime.now(timezone.utc) + timedelta(days=1):
            await run_blocking(self.queue, current, 'system', automatic=True)
        job = await run_blocking(self.claim)
        if not job:
            return
        if not current or current['id'] != job['id']:
            await run_blocking(self.finish, job, 'changed')
            return
        try:
            async with asyncio.timeout(self.settings.system_atr_call_timeout):
                await maintain_registered(config, current['aic'], 'renew', expected_certificate=current['id'])
                _, renewed = await run_blocking(self.snapshot)
                if (not renewed or renewed['aic'] != current['aic'] or renewed['id'] == current['id']
                        or datetime.fromisoformat(renewed['expires_at']) <= datetime.fromisoformat(current['expires_at'])):
                    raise ValueError('未读回有效的新证书')
        except (Exception, asyncio.CancelledError):
            await run_blocking(self.finish, job, 'unconfirmed')
            raise
        await run_blocking(self.finish, job, 'done')

    def recover(self):
        with self.database.connect(write=True) as db:
            job = self.job(db)
            if job.get('state') == 'running':
                self.save(db, {**job, 'state':'unconfirmed', 'updated_at':now()})

    async def run(self):
        recovered = False
        while True:
            self.wake.clear()
            try:
                if not recovered:
                    await run_blocking(self.recover)
                    recovered = True
                await self.check()
            except Exception as error:
                log.warning('平台证书检查未完成（%s）', type(error).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=RENEWAL_SCAN_SECONDS if recovered else 5)
            except TimeoutError:
                pass
