"""限长、脱敏的协议诊断，保留状态查询字段，不记录认证凭据。"""
import json
import hashlib
import logging
from logging.handlers import RotatingFileHandler
from avatar.core.db import now


def redact(value,depth=0):
    if depth>8:return '[depth limit]'
    if isinstance(value,dict):
        if value.get('type')=='file':
            value = {**value, **{key:'[redacted]' for key in ('uri','bytes') if key in value}}
        return {str(k):('[redacted]' if any(s in str(k).lower() for s in ('token','secret','password','authorization','cookie','privatekey')) else {'chars':len(v),'sha256':hashlib.sha256(v.encode()).hexdigest()} if str(k).lower() in ('text','content','body','description') and isinstance(v,str) else redact(v,depth+1)) for k,v in list(value.items())[:50]}
    if isinstance(value,(list,tuple)):return [redact(v,depth+1) for v in value[:20]]
    if isinstance(value,str):return value[:1024]
    return value


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        stream=super()._open()
        from pathlib import Path
        Path(self.baseFilename).chmod(0o600)
        return stream


class RpcDiagnostics:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.logger=logging.Logger('avatar-rpc-diagnostics')
        handler=PrivateRotatingHandler(path,maxBytes=1024*1024,backupCount=2,encoding='utf-8')
        path.chmod(0o600);self.logger.addHandler(handler)

    def write(self,**event):
        self.logger.warning(json.dumps({'at':now(),**redact(event)},ensure_ascii=False))

    def close(self):
        for handler in self.logger.handlers:handler.close()
        self.logger.handlers.clear()
