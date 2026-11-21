"""Host/cgroup observation and optional limits on this platform's own service."""
import os
from pathlib import Path
import subprocess
import psutil
from fastapi import HTTPException


def _number(path):
    try:
        value=path.read_text().strip()
        return None if value=='max' else int(value)
    except (OSError,ValueError):return None


def sample_resources():
    from avatar.core.resource_budget import ResourceSample
    memory=psutil.virtual_memory();process=psutil.Process()
    used=process.memory_info().rss
    for child in process.children(recursive=True):
        try:used+=child.memory_info().rss
        except (psutil.NoSuchProcess,psutil.AccessDenied):pass
    cpus=len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else os.cpu_count() or 1
    quotas=[];memory_limits=[];free_limits=[]
    try:
        relative=next(line.split(':',2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        root=Path('/sys/fs/cgroup').resolve();group=(root/relative.lstrip('/')).resolve()
        if group.is_relative_to(root):
            for directory in [group,*group.parents]:
                if not directory.is_relative_to(root):break
                try:
                    quota,period=(directory/'cpu.max').read_text().split()
                    if quota!='max':quotas.append(int(quota)/int(period))
                except (OSError,ValueError,ZeroDivisionError):pass
                limit=_number(directory/'memory.max');current=_number(directory/'memory.current')
                if limit is not None:
                    memory_limits.append(limit)
                    if current is not None:free_limits.append(max(0,limit-current))
    except (OSError,StopIteration):pass
    return ResourceSample(cpu_percent=psutil.cpu_percent(),cpu_cores=cpus,total_bytes=memory.total,
        available_bytes=memory.available,process_bytes=used,quota_cpu_cores=min(quotas) if quotas else None,
        quota_memory_bytes=min(memory_limits) if memory_limits else None,
        quota_available_bytes=min(free_limits) if free_limits else None)


class ServiceLimits:
    unit='personal-avatar.service'

    def apply(self,policy,sample):
        if policy.enforce_systemd and sample.process_bytes>policy.max_memory_gib*1024**3*.8:
            raise HTTPException(409,'当前进程及子进程占用接近新内存上限，请先收缩任务再调整')
        properties=([f'CPUQuota={policy.max_cpu_cores*100:g}%',
            f'MemoryHigh={int(policy.max_memory_gib*1024**3*.9)}',f'MemoryMax={int(policy.max_memory_gib*1024**3)}']
            if policy.enforce_systemd else ['CPUQuota=infinity','MemoryHigh=infinity','MemoryMax=infinity'])
        try:
            current=subprocess.run(['systemctl','--user','show',self.unit,'--property=MainPID','--value'],
                                   capture_output=True,text=True,timeout=5)
            if current.returncode or current.stdout.strip()!=str(os.getpid()):
                raise HTTPException(503,'当前进程不属于可管理的本服务，未修改任何系统限制')
        except (OSError,subprocess.TimeoutExpired):raise HTTPException(503,'无法确认当前服务身份，未修改任何系统限制') from None
        try:
            result=subprocess.run(['systemctl','--user','set-property','--runtime',self.unit,*properties],
                                  capture_output=True,text=True,timeout=8)
        except (OSError,subprocess.TimeoutExpired):raise HTTPException(503,'无法应用本服务的系统资源限制，配置未保存') from None
        if result.returncode:raise HTTPException(503,'systemd 未接受本服务的资源限制，配置未保存')
        if policy.enforce_systemd:
            observed=sample_resources()
            if (observed.quota_cpu_cores is None or observed.quota_cpu_cores>policy.max_cpu_cores+.01 or
                observed.quota_memory_bytes is None or observed.quota_memory_bytes>policy.max_memory_gib*1024**3):
                error=HTTPException(503,'系统已接受设置，但未确认实际硬限制；请检查本服务的 cgroup 权限')
                error.system_limit_changed=True
                raise error
        return {'mode':'systemd' if policy.enforce_systemd else 'soft','unit':self.unit,
                'scope':'整个平台及其子进程','error':''}
