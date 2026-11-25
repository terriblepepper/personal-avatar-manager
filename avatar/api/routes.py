"""Compose the HTTP API by domain."""
from fastapi import APIRouter

from avatar.api.platform_routes import router as platform_router
from avatar.api.auth_routes import router as auth_router
from avatar.api.profile_routes import router as profile_router
from avatar.api.chat_routes import router as chat_router
from avatar.api.admin_routes import router as admin_router
from avatar.api.attachment_routes import router as attachment_router
from avatar.api.model_routes import router as model_router


router = APIRouter(prefix="/api")
router.include_router(platform_router)
router.include_router(auth_router)
router.include_router(profile_router)
router.include_router(chat_router)
router.include_router(admin_router)
router.include_router(attachment_router)
router.include_router(model_router)
