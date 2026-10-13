"""Message payloads for Open WebUI's external app API."""

from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

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
    "channel.message.created",
    "channel.message.updated",
    "channel.message.deleted",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, revalidate_instances="always"
    )


class EmbedAuthor(StrictModel):
    name: str = Field(min_length=1, max_length=256)


class EmbedFooter(StrictModel):
    text: str = Field(min_length=1, max_length=2048)


class EmbedThumbnail(StrictModel):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or any(ord(c) <= 32 for c in value)
        ):
            raise ValueError("An HTTP(S) URL without credentials is required")
        return value


class EmbedField(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=1024)
    inline: bool = False


class Embed(StrictModel):
    title: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=4096)
    url: str | None = Field(default=None, max_length=2048)
    color: int | None = Field(default=None, strict=True, ge=0, le=0xFFFFFF)
    author: EmbedAuthor | None = None
    footer: EmbedFooter | None = None
    thumbnail: EmbedThumbnail | None = None
    fields: list[EmbedField] = Field(default_factory=list, max_length=25)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return EmbedThumbnail.validate_url(value) if value is not None else None

    def add_field(self, *, name: str, value: str, inline: bool = False):
        self.fields = [*self.fields, EmbedField(name=name, value=value, inline=inline)]
        return self


class Action(StrictModel):
    action_id: str | None = Field(default=None, min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2048)
    context: dict = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return EmbedThumbnail.validate_url(value) if value is not None else None

    @model_validator(mode="after")
    def check_action(self):
        if (self.action_id is None) == (self.url is None):
            raise ValueError("Provide exactly one of action_id or url")
        if self.url and self.context:
            raise ValueError("Link actions cannot have interaction context")
        return self


class AppChannelMessage(StrictModel):
    content: str = Field(default="", max_length=32000)
    actions: list[Action] = Field(default_factory=list, max_length=30)
    embeds: list[Embed] = Field(default_factory=list, max_length=10)
    reply_to_id: str | None = None
    parent_id: str | None = None

    @model_validator(mode="after")
    def check_embeds(self):
        total = sum(
            len(e.title)
            + len(e.description)
            + (len(e.author.name) if e.author else 0)
            + (len(e.footer.text) if e.footer else 0)
            + sum(len(f.name) + len(f.value) for f in e.fields)
            for e in self.embeds
        )
        if total > 6000:
            raise ValueError("Embeds must not exceed 6000 characters per message")
        return self


class AppChannelReply(AppChannelMessage):
    type: Literal["message", "update", "ack", "download", "error"] = "message"
    visibility: Literal["private", "channel"] = "private"
    title: str = Field(default="", max_length=200)
    filename: str = Field(default="report.md", pattern=r"^[a-zA-Z0-9_.-]{1,100}\.md$")

    @model_validator(mode="after")
    def check_reply(self):
        if self.embeds and self.type not in ("message", "update"):
            raise ValueError("Embeds require a message or update response")
        if self.actions and (
            self.visibility != "channel" or self.type not in ("message", "update")
        ):
            raise ValueError("Buttons require a channel message")
        if self.type == "ack" and (
            self.content
            or self.embeds
            or self.actions
            or self.title
            or self.parent_id
            or self.reply_to_id
        ):
            raise ValueError("Acknowledgements cannot contain a message")
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

    async def send(
        self,
        content: str = "",
        *,
        actions: list[Action] | None = None,
        embeds: list[Embed] | None = None,
    ) -> dict:
        """Post to this channel, preserving the existing thread."""
        return await self.client.create_message(
            self.channel_id,
            content=content,
            actions=actions,
            embeds=embeds,
            parent_id=self.parent_id,
        )

    async def reply(
        self,
        content: str = "",
        *,
        actions: list[Action] | None = None,
        embeds: list[Embed] | None = None,
    ) -> dict:
        """Post a channel-visible reply to the source message."""
        if self.type == "channel.message.deleted" or not self.message_id:
            raise ValueError("This event has no source message to reply to")
        return await self.client.create_message(
            self.channel_id,
            content=content,
            actions=actions,
            embeds=embeds,
            parent_id=self.parent_id,
            reply_to_id=self.message_id,
        )
