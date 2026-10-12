"""Message payloads for Open WebUI's external app API."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Scope = Literal["mentions:read", "messages:read", "messages:write", "channels:read"]
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
    webui_url: str
    actor: dict[str, Any] = Field(default_factory=dict)
    channel: dict[str, Any]
    data: dict[str, Any] = Field(default_factory=dict)
    message: dict[str, Any] | None = None
    execution_token: str | None = None
