"""Stand-ins for a LiveKit room and its participants: no LiveKit server, no network."""
import json


class FakeLocalParticipant:
    def __init__(self, reply="{\"ok\": true}"):
        self.reply, self.calls = reply, []

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout=None):
        self.calls.append((destination_identity, method, json.loads(payload)))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeParticipant:
    def __init__(self, identity, kind):
        self.identity, self.kind = identity, kind


class FakeRoom:
    def __init__(self, local, *participants):
        self.local_participant = local
        self.remote_participants = {p.identity: p for p in participants}
