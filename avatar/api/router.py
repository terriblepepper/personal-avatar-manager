"""The ordered HTTP route table. The SPA catch-all is mounted last by create_app."""
from fastapi import APIRouter

from avatar.api import (
    admin_routes, attachment_routes, auth_routes, chat_file_routes, chat_routes,
    collaboration_routes, diagram_routes, embedding_routes, entity_routes, knowledge_routes,
    migration_routes, model_routes, notification_routes, oidc_routes, ops_routes,
    platform_routes, profile_routes, question_bank_routes, rag_routes, resource_routes,
    review_routes, skill_routes, style_learning_routes, system_routes, town_routes, update_routes,
)

router = APIRouter()
for domain in (ops_routes, update_routes, migration_routes, town_routes):
    router.include_router(domain.router)
router.include_router(town_routes.account_router)
router.include_router(oidc_routes.router)
for domain in (platform_routes, auth_routes, profile_routes, chat_routes,
               admin_routes, attachment_routes, model_routes):
    router.include_router(domain.router, prefix='/api')
for domain in (knowledge_routes, style_learning_routes, embedding_routes, rag_routes,
               resource_routes, question_bank_routes, review_routes, diagram_routes,
               system_routes, entity_routes, skill_routes, collaboration_routes,
               notification_routes, chat_file_routes):
    router.include_router(domain.router)
