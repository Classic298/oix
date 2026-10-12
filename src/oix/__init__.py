"""Build external apps for Open WebUI."""

from .app import App
from .client import APIError, Client
from .models import Action, AppChannelMessage, AppChannelReply, Event

__all__ = [
    "APIError",
    "Action",
    "App",
    "AppChannelMessage",
    "AppChannelReply",
    "Client",
    "Event",
]
