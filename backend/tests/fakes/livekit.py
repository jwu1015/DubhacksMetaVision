"""Stand-ins for a LiveKit room, its participants and the server API: no LiveKit server, no network."""
import json
from types import SimpleNamespace

from livekit import rtc


class FakeLocalParticipant:
    """Records what it sends; answers perform_rpc with `reply` (an exception raises it)."""

    def __init__(self, reply="{\"ok\": true}"):
        self.reply, self.calls = reply, []
        self.sent: list[tuple[str, dict, list | None]] = []  # ("data" | "text", message, destination identities)
        self.rpc: dict = {}  # method -> handler

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout=None):
        self.calls.append((destination_identity, method, json.loads(payload)))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    async def publish_data(self, payload, *, reliable=True, destination_identities=(), topic=""):
        self.sent.append(("data", json.loads(payload), list(destination_identities) or None, topic))

    async def send_text(self, text, *, destination_identities=None, topic=""):
        self.sent.append(("text", json.loads(text), destination_identities, topic))

    def register_rpc_method(self, method, handler):
        self.rpc[method] = handler

    async def call(self, method: str, payload: dict | str, caller: str = "phone-1") -> dict:
        """Call a registered RPC method as `caller` would; its answer."""
        payload = payload if isinstance(payload, str) else json.dumps(payload)
        data = rtc.RpcInvocationData(request_id="r1", caller_identity=caller, payload=payload, response_timeout=10,
                                     method=method)
        return json.loads(await self.rpc[method](data))


class FakeParticipant:
    def __init__(self, identity, kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD):
        self.identity, self.kind = identity, kind


class FakeRoom:
    def __init__(self, local=None, *participants):
        self.local_participant = local or FakeLocalParticipant()
        self.remote_participants = {p.identity: p for p in participants}
        self.handlers: dict[str, list] = {}
        self.text_handlers: dict = {}
        self.disconnected = False

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for handler in self.handlers.get(event, []):
            handler(*args)

    def register_text_stream_handler(self, topic, handler):
        self.text_handlers[topic] = handler

    def join(self, p: FakeParticipant):
        self.remote_participants[p.identity] = p
        self.emit("participant_connected", p)

    def leave(self, identity: str):
        self.emit("participant_disconnected", self.remote_participants.pop(identity))

    async def disconnect(self):
        self.disconnected = True


class FakeTextReader:
    def __init__(self, text: str):
        self.text = text

    async def read_all(self) -> str:
        return self.text


class FakeLiveKitAPI:
    """The server API as Rooms and Session use it: records each call."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.room = SimpleNamespace(create_room=self._create_room, remove_participant=self._remove)
        self.agent_dispatch = SimpleNamespace(create_dispatch=self._dispatch, delete_dispatch=self._undispatch)

    def __call__(self):  # as a factory
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _create_room(self, req):
        self.calls.append(("create_room", req.name, req.empty_timeout))

    async def _remove(self, req):
        self.calls.append(("remove_participant", req.room, req.identity))

    async def _dispatch(self, req):
        self.calls.append(("create_dispatch", req.agent_name, req.room))
        return SimpleNamespace(id="d1")

    async def _undispatch(self, dispatch_id, room):
        self.calls.append(("delete_dispatch", dispatch_id, room))
