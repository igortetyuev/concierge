from fastapi import WebSocket

from concierge.channels.hub import ChannelEvent


class WebSocketSink:
    def __init__(self, ws: WebSocket, user_id: int) -> None:
        self._ws = ws
        self.user_id = user_id

    async def emit(self, event: ChannelEvent) -> None:
        await self._ws.send_json(
            {
                "type": event.type,
                "conversation_id": event.conversation_id,
                **event.data,
            }
        )
