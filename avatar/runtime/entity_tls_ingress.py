"""来源：Flux《单端口多实体 mTLS 入口.zip》component.py。
按 SNI 为每条连接固定独立实体、证书与 Endpoint；目录变化由 personal_aip 重建入口。
保留上游全局失败关闭策略，不在 TLS 同步回调中执行网络或文件操作。
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping
from dataclasses import dataclass

from wit.acps.aip.direct import DirectPartnerEndpoint
from avatar.protocols.transport.tls_listener import TlsListenerComponent
from wit.acps.identity import EntityDirectory, LocalEntity
from wit.components import Context


# 将一个正式 AIC 绑定到已启动的 Partner Endpoint，不在此启动额外监听器。
@dataclass(frozen=True)
class EntityBinding:
    aic: str
    endpoint: DirectPartnerEndpoint


# TLS 刷新时原子发布的不可变连接接纳快照。
@dataclass(frozen=True)
class _Prepared:
    entity: LocalEntity
    tls: ssl.SSLContext
    endpoint: DirectPartnerEndpoint


class EntityTlsIngressComponent(TlsListenerComponent):
    """One socket, mandatory SNI, a distinct certificate and AIP Endpoint per entity.

    Directory.select must return a freshly authorized, active identity snapshot.
    Use AtrEntityDirectory with real registration state outside this local probe.
    A refresh failure inherits the base's global fail-closed policy.
    """

    name = "experimental-entity-tls-ingress"

    # 校验域名/AIC 一对一及 Endpoint 主机名；绑定集在本次运行期间固定。
    def __init__(
        self,
        directory: EntityDirectory,
        bindings: Mapping[str, EntityBinding],
        **listener_options,
    ):
        super().__init__(directory, **listener_options)
        if not bindings:
            raise ValueError("at least one entity binding is required")
        normalized = {}
        aics = set()
        for hostname, binding in bindings.items():
            host = hostname.lower()
            if not host or host != host.strip() or ":" in host or "/" in host:
                raise ValueError("binding hostname must be a DNS name without port or path")
            if host in normalized or binding.aic in aics:
                raise ValueError("each hostname and AIC must have exactly one binding")
            if binding.endpoint.server_name.lower() != host:
                raise ValueError("binding hostname differs from Endpoint server_name")
            normalized[host] = binding
            aics.add(binding.aic)
        self._bindings = normalized
        self._prepared: dict[str, _Prepared] = {}

    # 逐实体核验有效身份，准备 SSLContext，全部成功后原子发布；失败交基类关闭接入。
    async def _refresh_tls(self, ctx: Context) -> None:
        prepared = {}
        for hostname, binding in self._bindings.items():
            entity = await self.directory.select(binding.aic)
            if entity.aic != binding.aic or entity.hostname.lower() != hostname:
                raise ValueError("directory snapshot differs from configured hostname/AIC")
            old = self._prepared.get(hostname)
            tls = (
                old.tls
                if old is not None and old.entity == entity
                else self.prepare_tls_context(entity)
            )
            prepared[hostname] = _Prepared(entity, tls, binding.endpoint)
        # Publish only a complete snapshot. SNI hooks do no I/O and never mutate TLS contexts.
        self._prepared = prepared

    # TLS 同步回调只查预备表；未携带 SNI 或未知域名均拒绝，不使用默认身份。
    def _select_entity(self, server_name: str | None) -> LocalEntity:
        if server_name is None or server_name not in self._prepared:
            raise LookupError("an explicitly registered SNI hostname is required")
        return self._prepared[server_name].entity

    # 确认连接选择的身份仍属于当前发布快照，防止刷新后复用过期 TLS 材料。
    def _prepared_for(self, entity: LocalEntity) -> _Prepared:
        prepared = self._prepared.get(entity.hostname.lower())
        if prepared is None or prepared.entity != entity:
            raise LookupError("entity is no longer in the prepared snapshot")
        return prepared

    # 返回该实体已准备的独立 SSLContext，不修改任何已发布上下文。
    def _select_tls_context(self, entity: LocalEntity) -> ssl.SSLContext:
        return self._prepared_for(entity).tls

    # 返回同一快照中绑定的 Partner Endpoint，不读取请求正文决定目标。
    def _select_endpoint(self, entity: LocalEntity) -> DirectPartnerEndpoint:
        return self._prepared_for(entity).endpoint
