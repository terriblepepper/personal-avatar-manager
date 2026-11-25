"""个人 ACS 独立注册：本平台批准后提交 Registry，审核通过再签证上线。"""
import asyncio
import json
import re

import httpx
from avatar.protocols.transport.certificates import registration_component
from wit import WitAgent
from wit.acps.atr import TRUSTED_REGISTRATION, RegistryClient
from avatar.protocols.transport.ready import RegistryReadyTransport
from wit.acps.identity.acs import REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME,validate_registration_candidate_acs
from avatar.protocols.identity.acs import resolve_input,build_acs,public_base_url,default_input
from avatar.protocols.registration.entity import EntityRegistrationRunner,entity_identity_paths,certificate_projection
from avatar.protocols.registration.base import RegistrationPending,RegistrationFailure
from avatar.protocols.identity.atr_config import read_atr_config
from avatar.protocols.identity.system import private_directory,create_identity_json,system_identity_paths
from avatar.protocols.identity.document import wit_candidate_config,sync_component_acs
from avatar.protocols.registration.system import plain_json,publish_identity_document
from avatar.protocols.transport.endpoints import entity_connection,entity_endpoint,entity_oidc_identity
from avatar.contracts.profile_fields import public_profile


def legacy_description(public):
    owner = public.get('name') or '该用户'
    text = f'作为{owner}的个人分身，根据已审核公开资料进行个人介绍、资料问答和已声明的技能服务。'
    if public.get('organization'):
        text += '所属学校或组织：' + public['organization'] + '。'
    if public.get('bio'):
        text += '个人简介：' + public['bio']
    return text[:2000]


def personal_input(stored, profile):
    public = public_profile(profile)
    stored = dict(stored or {})
    defaults = default_input(profile)
    if not stored.get('template_owner'):
        if stored.get('description') == legacy_description(public):
            stored['description'] = defaults.description
    content = resolve_input(stored, profile)
    required = defaults.skills[0]
    skills = [required] + [skill for skill in content.skills if skill.id != required.id]
    if len(skills) > 20:
        from fastapi import HTTPException
        raise HTTPException(422, '个人介绍问答为必备技能，最多可追加 19 条自定义技能')
    provider = content.provider.model_copy(update={'name': public.get('name',''), 'organization': public.get('organization','')})
    return content.model_copy(update={'skills': skills, 'provider': provider})


def signed_input(stored, document):
    """旧记录中的草稿说明可能已被重算，生效字段以签发的文档为准。"""
    result = dict(stored)
    for field in ('name', 'description'):
        if field in document:
            result[field] = document[field]
    descriptions = {s['id']: s['description'] for s in document.get('skills', [])}
    if 'skills' in result:
        result['skills'] = [{**s, 'description': descriptions.get(s['id'], s['description'])} for s in result['skills']]
    return result


def active_input(record):
    stored = signed_input(record['acs_input'],record['acs_document'])
    profile = dict(record['profile'])
    # 资料可在不换证的情况下改名；比对基准保留签发时的提供者信息。
    provider = stored.get('provider') or {}
    for field in ('name','organization'):
        if provider.get(field):profile[field]=provider[field]
    return personal_input(stored,profile)


async def verify_existing(settings,user_id,record):
    from avatar.protocols.identity.document import LocalEntityDirectory, identity_json
    paths=entity_identity_paths(settings.data_dir,user_id)
    try:
        config=read_atr_config(paths.registration)
        identity=await LocalEntityDirectory({record['aic']:config}).select(record['aic'])
        cert=record['certificate']
        if (cert.get('public_pem')!=identity.material.certificate
                or cert.get('server_public_pem')!=identity.material.server_certificate
                or record['acs_document']!=identity_json(paths.acs)):
            raise ValueError('identity mismatch')
    except (OSError,ValueError,LookupError) as exc:
        raise RegistrationFailure('复用核验失败：原 ACS、AIC 或双证书与当前身份材料不一致，请核对后重试',phase='identity_verification') from exc


def require_profile(profile):
    from fastapi import HTTPException
    public=public_profile(profile)
    for key,label in (('name','姓名'),('avatar_name','分身名称'),('bio','个人简介'),('organization','学校 / 组织')):
        if not str(profile.get(key,'')).strip():raise HTTPException(422,'实体注册前请填写'+label)
        if not public.get(key):raise HTTPException(422,'实体注册所需的'+label+'必须设置为公开')


def direct_candidate(settings,user_id,profile,stored):
    require_profile(profile)
    content=personal_input(stored,profile)
    if content.errors():raise RegistrationFailure('ACS 技能配置不完整：'+'；'.join(content.errors()))
    with entity_connection(settings, write=True) as db:
        base = public_base_url(db)
        endpoint = entity_endpoint(settings, user_id, db=db)
    if not endpoint:raise RegistrationFailure('个人实体通信端点尚未启用')
    content=content.model_copy(update={'icon_url':content.icon_url or base.rstrip('/')+'/static/system-avatar.svg','documentation_url':content.documentation_url or base.rstrip('/')+'/api/docs','provider':content.provider.model_copy(update={'url':content.provider.url or base})})
    doc=build_acs(content,user_id,base)
    doc.pop('webAppUrl',None)
    from urllib.parse import urlsplit
    import ipaddress
    host=urlsplit(endpoint['url']).hostname
    ipaddress.ip_address(host)
    doc.update(lastModifiedTime=REGISTRATION_CANDIDATE_LAST_MODIFIED_TIME,endPoints=[endpoint])
    doc['certificate']['altNames']={'dns':[],'ip':[host]}
    doc['entityMeta'].update(environment='1.0.0',registrationMode='direct',**entity_oidc_identity(settings,user_id))
    validate_registration_candidate_acs(doc)
    return doc


class DirectRegistrationRunner(EntityRegistrationRunner):
    async def configuration(self,user_id,profile,stored=None):
        settings=self.settings;paths=entity_identity_paths(settings.data_dir,user_id)
        if not settings.registry_username or not settings.registry_password:
            raise RegistrationPending('等待配置 Registry 注册凭据',phase='configuration')
        source=system_identity_paths(settings.data_dir).registration
        if not source.is_file():raise RegistrationFailure('缺少 Registry 和 CA 连接配置')
        infrastructure=read_atr_config(source,settings.registry_username,settings.registry_password)
        for directory in (paths.root.parent,paths.root,paths.certificates):private_directory(directory)
        candidate=direct_candidate(settings,user_id,profile,stored or {})
        if paths.acs.is_file() and not (stored or {}).get('template_owner'):
            from avatar.protocols.identity.document import identity_json
            saved = identity_json(paths.acs)
            if saved != candidate:
                public = public_profile(profile)
                descriptions = {s['id']: s['description'] for s in (stored or {}).get('skills', [])}
                legacy = dict(candidate, name=public.get('avatar_name') or '个人分身',
                              description=(stored or {}).get('description', legacy_description(public)),
                              skills=[{**s, 'description': s['description'].removeprefix((public.get('name') or '该用户')+'能够')
                                      if s['id']=='personal-profile.introduction.qa' else descriptions[s['id']]}
                                      for s in candidate['skills']])
                if all(saved.get(k) == legacy[k] for k in ('name', 'description', 'skills')):
                    candidate = legacy
        state_path=paths.certificates/'atr-state.json'
        if state_path.is_file():
            state=json.loads(state_path.read_text())
            if state.get('phase')=='rejected' and not state.get('aic') and not state.get('final_acs') and state.get('candidate')!=candidate:
                # A new platform-approved correction may replace an unsigned rejected attempt.
                import shutil
                shutil.rmtree(paths.certificates);private_directory(paths.certificates)
                paths.acs.unlink(missing_ok=True);paths.registration.unlink(missing_ok=True)
        create_identity_json(paths.acs,candidate)
        create_identity_json(paths.registration,{
            'registry_url':infrastructure.endpoints.registry_url,
            'certificate_authority_url':infrastructure.endpoints.certificate_authority_url,
            'ca_cert_path':str(infrastructure.endpoints.ca_cert_path.resolve()),
            'acs_path':'acs.json','state_dir':'certificates','mode':'integrated','use_saved_urls':False})
        config=read_atr_config(paths.registration,settings.registry_username,settings.registry_password)
        if config.mode!='integrated' or config.paths.parent_state_dir:
            raise RegistrationFailure('该账户仍有旧派生注册状态，需完成身份迁移后再提交独立实体')
        from avatar.protocols.identity.document import registration_candidate
        original=registration_candidate(config)
        # 旧申请继续按原冻结 ACS 核验；仅兼容本次移除的可选网页字段。
        if original.get('webAppUrl') and 'webAppUrl' not in candidate:
            original={key:value for key,value in original.items() if key!='webAppUrl'}
        if original.get('entityMeta',{}).get('environment') in ('demo','production'):
            original={**original,'entityMeta':{**original['entityMeta'],'environment':'1.0.0'}}
        if original!=candidate:
            raise RegistrationFailure('当前候选与正在审核的 ACS 不同；保留原申请，禁止自动覆盖或重复注册')
        return paths,config

    async def advance(self,user_id,profile,stored=None):
        async with asyncio.timeout(self.settings.system_atr_call_timeout):
            paths,config=await self.configuration(user_id,profile,stored)
            with wit_candidate_config(config) as input_config:
                component=registration_component(input_config,registry=RegistryClient(input_config,transport=RegistryReadyTransport(input_config)))
                agent=WitAgent(metadata={'application':'personal-avatar-manager','entry':'direct-personal-registration'})
                agent.install(component,required=True)
                try:
                    async with agent:
                        result=await agent.context.get(TRUSTED_REGISTRATION).up()
                        phase=str(result.phase)
                        if phase in ('blocked','rejected'):
                            raise RegistrationFailure('Registry 审核已拒绝或申请受阻，请管理员核对该申请',phase='registry_rejected' if phase=='rejected' else 'registry_blocked')
                        if phase!='active':
                            stage='registry_review' if phase=='pending_review' else 'certificate_issuing' if phase=='approved' else 'registry_submitting'
                            raise RegistrationPending({'registry_review':'等待 Registry 管理员审核','certificate_issuing':'Registry 已通过，正在申请并核验证书','registry_submitting':'正在向 Registry 提交实体申请'}[stage],self.settings.system_atr_poll_interval,phase=stage)
                        if not result.aic or component.final_acs is None:raise RegistrationFailure('未取得正式 ACS，不能标记上线')
                        document=plain_json(component.final_acs)
                finally:sync_component_acs(config,component)
            publish_identity_document(paths.acs,document,result.aic,user_id)
            with wit_candidate_config(config) as input_config:
                material,generation,_=await registration_component(input_config).read_active_snapshot()
            if (material.aic!=document['aic'] or document.get('entityUserId')!=user_id or document.get('active') is not True):raise RegistrationFailure('证书与正式 ACS 不一致，拒绝上线')
            certificate=certificate_projection(material,generation,'');certificate.update(mode='acps-direct');certificate.pop('ontology_aic',None)
            return {'aic':material.aic,'certificate':certificate,'acs_document':document}


class DirectRegistrationProvider:
    mode='acps-direct'
    def __init__(self,settings):self.runner=DirectRegistrationRunner(settings)

    # 职责：为首次审核通过的用户注册身份，已有用户只更新业务资料。
    # 输入：user_id 稳定不变，profile 为已审核公开/私密资料快照，previous 为原分身。
    # 结果与约束：不向 Registry 发送私密资料；正式声明的修改必须按平台规范单独处理；遇到协议不兼容保留远端 AIC 等待对账。
    async def provision(self, user_id, profile, previous, *, acs_input=None):
        if previous:
            if not previous['aic'].startswith('DEMO-') and any(
                    str(item.get('id', '')).startswith('avatar.manager.') for item in previous.get('acs_document', {}).get('skills', [])):
                raise RegistrationFailure('已有正式分身继承了管理技能，请保留原 AIC 并按平台规范迁移；不会自动替换身份')
            return {'aic': previous['aic'], 'certificate': previous['certificate'], 'acs_document': previous.get('acs_document', {})}
        try:
            return await self.runner.advance(user_id, profile, stored=acs_input)
        except (RegistrationFailure, RegistrationPending):
            raise
        except (OSError, httpx.HTTPError) as exc:
            if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code==429:
                raise RegistrationPending('注册或证书服务限流（HTTP 429），保留原 AIC 和状态，稍后自动重试', max(60,self.runner.settings.system_atr_poll_interval)) from None
            raise RegistrationPending('注册网络暂不可用或响应超时；保留状态并恢复，结果不确定时需对账', self.runner.settings.system_atr_poll_interval) from None
        except RuntimeError as exc:
            # ATR 将 ACME HTTP 状态包装为 RuntimeError；仅明确的临时响应继续原申请。
            transient = re.match(r'^ACME HTTP (429|502|503|504)\b', str(exc))
            if transient:
                code = transient.group(1)
                message = '证书服务限流' if code == '429' else '证书服务暂不可用'
                raise RegistrationPending(message+'（HTTP '+code+'），保留原 AIC 和签证状态，稍后自动重试', max(60,self.runner.settings.system_atr_poll_interval)) from None
            raise RegistrationFailure('Registry 正式 ACS 与 wit 严格契约不一致或状态受阻；请保留 AIC 和状态，核对服务端正式字段后再对账') from None
        except (ValueError, LookupError):
            raise RegistrationFailure('Registry 正式 ACS 与 wit 严格契约不一致或状态受阻；请保留 AIC 和状态，核对服务端正式字段后再对账') from None

    # 职责：拒绝用同步演示接口覆盖正式证书。
    # 输入：certificate 是当前证书公开元数据。
    # 结果与约束：正式续期需使用该账户 registration.json 的 wit 工具，不在 数据库写事务内进行远端签发。
    def renew(self, certificate):
        if certificate.get('mode') == 'demo':
            from avatar.protocols.registration.base import DemoRegistrationProvider
            return DemoRegistrationProvider().renew(certificate)
        from fastapi import HTTPException
        raise HTTPException(409, '请使用该实体 registration.json 运行 wit 证书续期工具')
