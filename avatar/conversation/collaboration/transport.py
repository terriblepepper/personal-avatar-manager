"""Delivery mode and pre-send transport failures shared by peer adapters."""
from fastapi import HTTPException


class PeerEndpointUnavailable(HTTPException):
    """仅发送前检查可抛出；尚未调用对端，允许等待入口恢复。"""
    def __init__(self,detail):super().__init__(503,detail)


def current_protocol(own_aic,target_aic,*,ready=False):
    return 'acps' if ready or all(not aic.startswith('DEMO-') for aic in (own_aic,target_aic)) else 'internal'
