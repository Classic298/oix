"""Run: python examples/hello.py"""

import os
from pathlib import Path

from oix import App, AppReply, Client, Event

app = App(
    id="hello",
    name="Hello",
    api_key=os.environ["API_KEY"],
    private_key=Path(os.environ["APP_PRIVATE_KEY_FILE"]).read_text(),
    scopes=["mentions:read"],
    allowed_private_origins=tuple(
        filter(
            None,
            os.getenv(
                "OPEN_WEBUI_ALLOWED_PRIVATE_ORIGINS",
                "",
            ).split(","),
        )
    ),
)


@app.on("mention")
async def hello(event: Event, client: Client) -> AppReply:
    return AppReply(content="Hello! This reply is only visible to you.")


if __name__ == "__main__":
    app.run()
