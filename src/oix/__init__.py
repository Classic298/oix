"""Build external apps for Open WebUI."""

from .app import App
from .client import APIError, Client, ConnectionLost, ConnectionUnavailable
from .models import Action, AppChannelMessage, AppChannelReply, Embed, EmbedField, Event

__all__ = [
    "APIError",
    "Action",
    "App",
    "AppChannelMessage",
    "AppChannelReply",
    "Client",
    "ConnectionLost",
    "ConnectionUnavailable",
    "Embed",
    "EmbedField",
    "Event",
]
