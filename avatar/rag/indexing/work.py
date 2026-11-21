"""File indexing and Soul share per-account and adaptive global admission."""
from contextlib import asynccontextmanager
from avatar.core.resource_budget import ResourceController

INDEX_WORKERS=10


class AccountIndexWork:
    def __init__(self,resources=None):self.resources=resources or ResourceController()

    @asynccontextmanager
    async def slot(self,user_id,stage='index'):
        async with self.resources.slot(user_id,stage):yield

    async def execute(self,user_id,stage,function,*args):
        from avatar.files.uploads.buffer import upload_work
        async with self.resources.slot(user_id,stage):return await upload_work(function,*args)
