"""Build external apps for Open WebUI."""

from .app import App
from .client import APIError, Client, ConnectionLost, ConnectionUnavailable
from .models import Action, AppChannelMessage, AppChannelReply, Event

__all__ = [
    "APIError",
    "Action",
    "App",
    "AppChannelMessage",
    "AppChannelReply",
    "Client",
    "ConnectionLost",
    "ConnectionUnavailable",
    "Event",
]
