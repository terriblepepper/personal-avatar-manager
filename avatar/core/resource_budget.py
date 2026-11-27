"""Adaptive, fair admission for a shared host; budgets do not claim OS enforcement."""
import asyncio
from collections import Counter,OrderedDict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict
from avatar.core.resource_limits import ResourceSample, sample_resources
import json
import math
import time
from pydantic import BaseModel,ConfigDict,Field,model_validator
from fastapi import HTTPException

GiB=1024**3
STAGES=('index','query','embedding','vision','rerank','parse')


class ResourcePolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    max_cpu_cores:float=Field(default=40,ge=.1,le=1024)
    max_memory_gib:float=Field(default=16,ge=1,le=4096)
    cache_gib:float=Field(default=8,ge=.01,le=2048)
    reserve_memory_gib:float=Field(default=12,ge=0,le=4096)
    host_cpu_target:float=Field(default=70,ge=10,le=95)
    max_jobs:int=Field(default=40,ge=2,le=512)
    query_reserved:int=Field(default=4,ge=1,le=64)
    max_index_per_user:int=Field(default=10,ge=1,le=10)
    soul_concurrency:int=Field(default=4,ge=1,le=64)
    query_concurrency:int=Field(default=16,ge=1,le=128)
    embedding_concurrency:int=Field(default=16,ge=1,le=128)
    vision_concurrency:int=Field(default=4,ge=1,le=64)
    rerank_concurrency:int=Field(default=8,ge=1,le=64)
    parse_concurrency:int=Field(default=16,ge=1,le=128)
    queue_limit:int=Field(default=4096,ge=1,le=100000)
    query_wait_seconds:float=Field(default=15,ge=.1,le=120)
    sample_seconds:float=Field(default=2,ge=.25,le=30)
    enforce_systemd:bool=False

    @model_validator(mode='after')
    def bounds(self):
        if self.query_reserved>=self.max_jobs:raise ValueError('查询保留额度必须小于全局任务上限')
        if self.cache_gib>self.max_memory_gib*.75:raise ValueError('缓存不能超过内存预算的75%')
        return self


class ResourceController:
    def __init__(self,policy=None,*,sampler=None):
        self.policy=policy or ResourcePolicy();self.sampler=sampler or sample_resources
        self.condition=asyncio.Condition();self.settings_lock=asyncio.Lock()
        self.active=Counter();self.index_users=Counter();self.active_jobs=0
        self.waiters=OrderedDict();self.sequence=0
        self.held=ContextVar('rag_resource_scope',default=None)
        self.current_jobs=min(self.policy.max_jobs,self.policy.query_reserved+1)
        self.enforcement={'mode':'soft','scope':'应用预算；不是操作系统硬上限','error':''}
        self.update(self.sampler())

    @classmethod
    def from_database(cls,database):
        with database.connect() as db:row=db.execute("SELECT value FROM settings WHERE key='rag_resource_policy'").fetchone()
        return cls(ResourcePolicy.model_validate_json(row[0]) if row else None)

    def update(self,sample):
        self.sample=sample;p=self.policy
        self.cpu=min(p.max_cpu_cores,sample.cpu_cores,sample.quota_cpu_cores or math.inf)
        self.memory=min(int(p.max_memory_gib*GiB),sample.total_bytes,sample.quota_memory_bytes or math.inf)
        available=min(sample.available_bytes,sample.quota_available_bytes if sample.quota_available_bytes is not None else math.inf)
        # Host reserve protects other users; cgroup reserve is proportional to its own ceiling.
        reserve=min(p.reserve_memory_gib*GiB,sample.total_bytes*.4)
        self.pressure=(sample.cpu_percent>=p.host_cpu_target or sample.available_bytes<reserve or
                       sample.process_bytes>self.memory*.85 or available<min(GiB,self.memory*.1) or sample.loop_delay>.2)
        ceiling=min(p.max_jobs,max(2,math.ceil(self.cpu*2)))
        if self.pressure:self.current_jobs=max(1,self.current_jobs//2)
        else:self.current_jobs=min(ceiling,self.current_jobs+1)
        self.current_jobs=min(ceiling,self.current_jobs)
        self.reserved=min(p.query_reserved,max(1,self.current_jobs//2))
        reserved=self.reserved
        self.limits={'index':max(0,self.current_jobs-reserved),'query':min(p.query_concurrency,self.current_jobs),
                     'embedding':min(p.embedding_concurrency,self.current_jobs),
                     'vision':min(p.vision_concurrency,self.current_jobs),
                     'rerank':min(p.rerank_concurrency,self.current_jobs),
                     'parse':min(p.parse_concurrency,max(1,math.floor(self.cpu)),self.current_jobs)}
        self.cache_budget=max(0,min(int(p.cache_gib*GiB),self.memory//2,
            int(max(0,sample.available_bytes-reserve)),int(available)))
        if self.pressure:self.cache_budget=min(self.cache_budget,max(0,(self.memory-sample.process_bytes)//2))

    def status(self):
        return {'policy':self.policy.model_dump(),'limits':dict(self.limits),'active':dict(self.active),
                'active_jobs':self.active_jobs,'waiting':len(self.waiters),'pressure':self.pressure,
                'cache_budget_bytes':self.cache_budget,'effective_cpu_cores':self.cpu,
                'effective_memory_bytes':self.memory,'sample':asdict(self.sample),'enforcement':dict(self.enforcement)}

    def available(self,stage='index'):
        return max(0,min(self.limits[stage]-self.active[stage],
            self.current_jobs-self.active_jobs-(self.reserved if stage!='query' else 0)))

    def try_reserve(self,owner,stage='index'):
        """Reserve on the event-loop thread before constructing a worker task."""
        entry=(owner,stage,False,stage=='query')
        if any(self._can_start(value) for value in self.waiters.values()) or not self._can_start(entry):return None
        self.active[stage]+=1;self.active_jobs+=1
        if stage=='index':self.index_users[owner]+=1
        return ResourceLease(self,owner,stage)

    def _can_start(self,entry):
        owner,stage,nested,is_query=entry
        cap=self.limits[stage]
        if stage not in ('index','query') and not is_query:cap=max(1,cap-min(self.policy.query_reserved,max(1,cap//4)))
        if self.active[stage]>=cap:return False
        if not nested:
            reserve=0 if is_query else self.reserved
            if self.active_jobs>=self.current_jobs-reserve:return False
            if stage=='index' and self.index_users[owner]>=self.policy.max_index_per_user:return False
        return True

    @asynccontextmanager
    async def slot(self,owner,stage='index'):
        if stage not in STAGES:raise ValueError('Unknown resource stage')
        task=asyncio.current_task();held=self.held.get()
        nested=bool(held and held[0] is task and held[1]==owner)
        stages=held[2] if nested else frozenset()
        if stage in stages:
            yield
            return
        is_query=stage=='query' or 'query' in stages
        entry=(owner,stage,nested,is_query);granted=False
        deadline=time.monotonic()+self.policy.query_wait_seconds if is_query else math.inf
        async with self.condition:
            if len(self.waiters)>=self.policy.queue_limit:raise HTTPException(429,'资源等待队列已满，请稍后重试')
            self.sequence+=1;ticket=self.sequence;self.waiters[ticket]=entry
            try:
                while True:
                    first=next((key for key,value in self.waiters.items() if self._can_start(value)),None)
                    if first==ticket:
                        self.active[stage]+=1
                        if not nested:
                            self.active_jobs+=1
                            if stage=='index':self.index_users[owner]+=1
                        granted=True;break
                    if time.monotonic()>=deadline:raise HTTPException(429,'检索资源繁忙，请稍后重试；后台任务继续执行')
                    try:await asyncio.wait_for(self.condition.wait(),min(.2,max(.001,deadline-time.monotonic())))
                    except TimeoutError:pass
            finally:self.waiters.pop(ticket,None);self.condition.notify_all()
        token=self.held.set((task,owner,stages|{stage}))
        try:yield
        finally:
            self.held.reset(token)
            if granted:
                async with self.condition:
                    self.active[stage]-=1
                    if not nested:
                        self.active_jobs-=1
                        if stage=='index':self.index_users[owner]-=1
                    self.condition.notify_all()

    async def run(self):
        while True:
            started=time.monotonic();await asyncio.sleep(self.policy.sample_seconds)
            from dataclasses import replace
            delay=max(0,time.monotonic()-started-self.policy.sample_seconds)
            sample=await asyncio.to_thread(self.sampler)
            self.update(replace(sample,loop_delay=delay))
            async with self.condition:self.condition.notify_all()


class ResourceLease:
    def __init__(self,controller,owner,stage):
        self.controller,self.owner,self.stage=controller,owner,stage
        self.closed=False

    async def __aenter__(self):
        if self.closed:raise RuntimeError('Resource reservation already released')
        self.token=self.controller.held.set((asyncio.current_task(),self.owner,frozenset({self.stage})))
        return self

    def release(self):
        if self.closed:return
        self.closed=True;c=self.controller
        c.active[self.stage]-=1;c.active_jobs-=1
        if self.stage=='index':c.index_users[self.owner]-=1

    async def __aexit__(self,*args):
        self.controller.held.reset(self.token);self.release()
        async with self.controller.condition:self.controller.condition.notify_all()
