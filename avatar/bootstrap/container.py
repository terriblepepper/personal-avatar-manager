"""Compose the domain services shared by HTTP, WIT and the protocol listeners."""
from avatar.accounts.identity_migration import IdentityMigrationService
from avatar.accounts.oidc import OidcService
from avatar.conversation.collaboration.dialogues import AgentDialogues
from avatar.conversation.jobs.runner import GenerationRunner
from avatar.core.config import require_database_url, runtime_environment
from avatar.core.dispatch import Changes
from avatar.core.monitoring import PlatformMonitor
from avatar.knowledge.build_worker import SoulBuildWorker
from avatar.knowledge.service import KnowledgeService
from avatar.models.probes import CapabilityProbes
from avatar.profiles.entity_lifecycle import EntityLifecycleService
from avatar.profiles.pending_reviews import PendingReviews
from avatar.protocols.tasks.dialogues import PeerDialogueTasks
from avatar.protocols.transport.peer import PeerTransport
from avatar.runtime.personal_aip import PersonalAipRuntime
from avatar.runtime.platform_certificate import PlatformCertificate
from avatar.runtime.system_aip import SystemAipRuntime
from avatar.runtime.system_registration import SystemRegistrationRuntime
from avatar.runtime.wit_runtime import AvatarRuntime


def configure(state, settings, provider, run_worker, model_client):
    require_database_url(settings.database_url)
    state.settings = settings
    state.runtime = AvatarRuntime(settings, provider, run_worker, model_client)
    resources = state.runtime.services
    # HTTP 管理界面与 wit Entry 共用领域对象；兼容测试注入模型客户端，不建立第二套数据或认证。
    state.database = resources.database
    state.service = resources.profiles
    state.worker = resources.worker
    state.chat = resources.chat
    state.rag_resources = state.chat.rag.resources
    state.environment = runtime_environment(settings, resources.provider)

    state.pending_reviews = PendingReviews(state.database, settings.redis_socket)
    state.changes = Changes(state.database, settings.redis_socket, settings.kafka_bootstrap,
                           state.pending_reviews.invalidate)
    state.database.changes = state.changes
    state.monitor = PlatformMonitor(state.database, state.changes, state.pending_reviews.client)
    state.generations = GenerationRunner(state.runtime, state.database)
    state.knowledge = KnowledgeService(state.database, state.chat)
    state.service.knowledge = state.knowledge
    state.soul_builds = SoulBuildWorker(state.knowledge)
    state.capability_probes = CapabilityProbes()
    state.oidc = OidcService(state.database, settings)
    state.identity_migration = IdentityMigrationService(state.database, settings)

    state.system_aip = SystemAipRuntime(settings, state.database)
    state.system_registration = SystemRegistrationRuntime(settings, state.system_aip)
    state.platform_certificate = PlatformCertificate(state.database, settings)
    state.entity_lifecycle = EntityLifecycleService(state.database, settings)
    state.personal_aip = PersonalAipRuntime(settings, state.chat)
    state.dialogues = AgentDialogues(state.database, state.chat)
    state.chat.dialogues = state.dialogues
    state.dialogues.transport = PeerTransport(state.personal_aip, state.dialogues)
    state.personal_aip.tasks.peer_dialogues = PeerDialogueTasks(state.dialogues, settings)
