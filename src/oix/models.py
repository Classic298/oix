"""Message payloads for Open WebUI's external app API."""

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

if TYPE_CHECKING:
    from .client import Client

Scope = Literal[
    "channels:mentions:read",
    "channels:messages:read",
    "channels:messages:write",
    "channels:read",
]
EventType = Literal[
    "channel.mention",
    "channel.action",
    "channel.operation.status",
    "channel.message.created",
    "channel.message.updated",
    "channel.message.deleted",
]


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    context: dict[str, Any] = Field(default_factory=dict)


class AppChannelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(default="", max_length=32000)
    actions: list[Action] = Field(default_factory=list, max_length=30)
    reply_to_id: str | None = None
    parent_id: str | None = None


class AppChannelReply(AppChannelMessage):
    type: Literal["message", "update", "deferred", "download", "error"] = "message"
    visibility: Literal["private", "channel"] = "private"
    title: str = Field(default="", max_length=200)
    filename: str = Field(default="report.md", pattern=r"^[a-zA-Z0-9_.-]{1,100}\.md$")
    operation_id: str | None = None

    @model_validator(mode="after")
    def check_reply(self):
        if self.actions and (
            self.visibility != "channel" or self.type not in ("message", "update")
        ):
            raise ValueError("Buttons require a channel message")
        if self.type == "deferred" and not self.operation_id:
            raise ValueError("Deferred replies require an operation ID")
        return self


class Event(BaseModel):
    """Verified event fields. Execution tokens can be rechecked before later work."""

    id: str = Field(min_length=1, max_length=100)
    type: EventType
    app_id: str = Field(min_length=1, max_length=100)
    installation_id: str
    actor: dict[str, Any] = Field(default_factory=dict)
    channel: dict[str, Any]
    data: dict[str, Any] = Field(default_factory=dict)
    message: dict[str, Any] | None = None
    execution_token: str | None = None

    _client: "Client | None" = PrivateAttr(default=None)

    @property
    def client(self) -> "Client":
        if self._client is None:
            raise RuntimeError("This event has no verified client")
        return self._client

    @property
    def content(self) -> str | None:
        if self.type in ("channel.message.created", "channel.message.updated"):
            return (self.message or {}).get("content")
        if self.type == "channel.mention":
            return self.data.get("text")
        return None

    @property
    def channel_id(self) -> str:
        return self.channel["id"]

    @property
    def message_id(self) -> str | None:
        if self.type.startswith("channel.message."):
            return (self.message or {}).get("id")
        if self.type in ("channel.mention", "channel.action"):
            return self.data.get("message_id")
        return None

    @property
    def parent_id(self) -> str | None:
        if self.type.startswith("channel.message."):
            return (self.message or {}).get("parent_id")
        if self.type in ("channel.mention", "channel.action"):
            return self.data.get("parent_id")
        return None

    async def send(self, content: str, *, actions: list[Action] | None = None) -> dict:
        """Post to this channel, preserving the existing thread."""
        return await self.client.create_message(
            self.channel_id, content=content, actions=actions, parent_id=self.parent_id
        )

    async def reply(self, content: str, *, actions: list[Action] | None = None) -> dict:
        """Post a channel-visible reply to the source message."""
        if self.type == "channel.message.deleted" or not self.message_id:
            raise ValueError("This event has no source message to reply to")
        return await self.client.create_message(
            self.channel_id,
            content=content,
            actions=actions,
            parent_id=self.parent_id,
            reply_to_id=self.message_id,
        )
