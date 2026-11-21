# 模块说明：公开系统 ACS 和协议状态；普通 Web 端口拒绝 RPC，不将请求头或请求体中的声明当作 TLS 身份。
import json
from fastapi import APIRouter, Request, HTTPException, Depends
from avatar.accounts.auth import current_user
from avatar.protocols.identity.system import build_system_acs, system_identity_paths
from avatar.protocols.identity.ontology import avatar_ontology_paths, build_avatar_ontology_acs, validate_avatar_ontology

router = APIRouter(tags=['系统协议'])


# 职责：提供管理系统唯一服务身份 ACS 的只读下载入口。
# 输入：request 用于读取服务端数据目录和部署地址，不接收用户指定文件路径。
# 结果与约束：读取唯一 acs.json：注册前为候选，注册后为正式文档；端点直接在该 ACS 声明，不创建额外管理身份或预览文件。
@router.get('/api/system/acs')
def system_acs(request: Request):
    settings = request.app.state.settings
    paths = system_identity_paths(settings.data_dir)
    path = paths.acs
    try:
        return json.loads(path.read_text()) if path.exists() else build_system_acs(settings.oidc_public_url, settings.system_aip_public_url)
    except (ValueError, OSError):
        raise HTTPException(503, '系统 ACS 暂不可用') from None


# 职责：提供独立个人分身本体的公开 ACS。
# 输入：使用服务器的数据目录和公开 Web 地址，不接受用户身份或文件路径。
# 结果与约束：返回个人问答能力模板或其正式文档；不创建身份，不返回私钥、注册配置或任何用户资料。
@router.get('/api/avatar-ontology/acs')
def avatar_ontology_acs(request: Request):
    settings = request.app.state.settings
    if settings.provider=='acps-direct':raise HTTPException(410,'个人分身已改为独立实体注册，不再使用个人能力本体')
    paths = avatar_ontology_paths(settings.data_dir)
    path = paths.acs
    try:
        document = json.loads(path.read_text()) if path.exists() else build_avatar_ontology_acs(settings.oidc_public_url)
        validate_avatar_ontology(document)
        return document
    except (ValueError, OSError):
        raise HTTPException(503, '个人分身本体 ACS 暂不可用') from None


# 职责：提供系统 mTLS 端点的真实就绪状态。
# 输入：request 引用应用级 SystemAipRuntime。
# 结果与约束：关闭或身份不可用时 serving=false，不声称已接入小镇；不公开私密配置。
@router.get('/api/system/aip/status')
def system_aip_status(request: Request):
    result = request.app.state.system_aip.status()
    registration = request.app.state.system_registration.status()
    result['registration'] = {key: registration[key] for key in ('enabled', 'running', 'phase', 'updated_at')}
    personal = request.app.state.avatar_registration.status()
    result['avatar_ontology'] = {key: personal[key] for key in ('enabled', 'running', 'phase', 'updated_at')}
    return result


# 职责：向根管理员显示真实系统注册阶段和排障提示。
# 输入：当前完整会话，权限由服务端认证依赖及 is_root 检查共同控制。
# 结果与约束：普通管理员、普通用户和临时入口均不能读取注册 ID 或审核原因；接口只读，不触发远端审批/注册。
@router.get('/api/admin/system/registration')
def system_registration_status(request: Request, user=Depends(current_user)):
    if not user.get('is_root') or user.get('access_scope', 'full') != 'full':
        raise HTTPException(403, '仅根管理员可以查看系统可信注册状态')
    result = request.app.state.system_registration.status()
    result['personal_registration_mode']=request.app.state.settings.provider
    if request.app.state.settings.provider!='acps-direct':result['avatar_ontology'] = request.app.state.avatar_registration.status()
    return result


@router.post('/api/admin/system/registration/refresh', status_code=202)
async def refresh_system_registration(request: Request, user=Depends(current_user)):
    if not user.get('is_root') or user.get('access_scope', 'full') != 'full':
        raise HTTPException(403, '仅根管理员可以刷新系统可信注册状态')
    await request.app.state.system_registration.refresh()
    if request.app.state.settings.provider!='acps-direct':
        await request.app.state.avatar_registration.refresh()
    return system_registration_status(request, user)


def certificate_admin(user=Depends(current_user)):
    if not user.get('is_root') or user.get('access_scope', 'full') != 'full':
        raise HTTPException(403, '仅根管理员可管理平台证书')
    return user


@router.get('/api/admin/system/certificate')
def platform_certificate(request: Request, user=Depends(certificate_admin)):
    return request.app.state.platform_certificate.status()


@router.post('/api/admin/system/certificate/{fingerprint}/renew', status_code=202)
async def renew_platform_certificate(fingerprint: str, request: Request, user=Depends(certificate_admin)):
    await request.app.state.platform_certificate.request(fingerprint, user['username'])
    return {'status':'accepted'}


# 职责：拒绝通过普通 Web HTTP 端口冒用 AIP RPC。
# 输入：不读取正文，不信任 X-Client-Cert、X-Peer-AIC 等伪造身份头。
# 结果与约束：固定拒绝；真实 RPC 由独立 wit mTLS listener 处理，不能借此绕过客户端证书认证。
@router.post('/aip/rpc')
def reject_plaintext_rpc():
    raise HTTPException(403, '系统 RPC 仅接受独立 HTTPS mTLS 端点，请查看 /api/system/aip/status')
