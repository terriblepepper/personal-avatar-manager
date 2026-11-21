"""Only index work retries transient model transport failures; chat sends stay single-shot."""
import asyncio
from fastapi import HTTPException


class IndexRetry:
    def __init__(self,notify):
        self.notify=notify
        self.used=0

    async def run(self,operation):
        while True:
            try:return await operation()
            except HTTPException as error:
                if not getattr(error,'retryable',False):raise
                if self.used>=3:
                    error.index_retries_exhausted=True
                    detail=str(error.detail)
                    for text in ('。系统未自动重试','，系统不会自动重试','，未自动重试','；未重发 HTTP 请求'):
                        detail=detail.replace(text,'')
                    error.detail='索引已用完 3 次自动重试机会；'+detail
                    raise
                self.used+=1
                delay=(2,5,10)[self.used-1]
                self.notify(self.used,delay)
                await asyncio.sleep(delay)
