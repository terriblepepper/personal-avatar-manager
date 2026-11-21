# 模块说明：注册与证书诊断的公开错误摘要，保留定位信息，不输出响应正文或私密状态。
import re
import ssl
import httpx
from wit.acps.identity.acs import AcsContractError


# 职责：将注册/证书异常转换为管理员可转发的安全错误。
# 输入：exc 是实际异常；不接收凭据、私钥或未经筛选的响应对象。
# 结果与约束：ACS 保留字段路径及固定缺字段消息，HTTP 只含状态码，TLS 只含验证码；未知错误不输出异常正文。
def identity_error_detail(exc):
    kind = type(exc).__name__
    if isinstance(exc, AcsContractError):
        path = str(exc.path)
        if len(path) > 200 or not re.fullmatch(r'\$[A-Za-z0-9_.\[\]-]*', path):
            path = '$'
        message = 'required field is missing' if exc.message == 'required field is missing' else 'field validation failed'
        return f'{kind}: ACS contract violation at {path}: {message}'
    if isinstance(exc, httpx.HTTPStatusError):
        return f'{kind}: HTTP {exc.response.status_code}'
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f'{kind}: TLS certificate verification failed (code={getattr(exc, "verify_code", "unknown")})'
    return kind
