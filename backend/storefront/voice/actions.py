"""The voice agent's calls: the phone's RPC "action", and the backend's "select" (a Look, as a tap starts).

The phone's "action":

    payload  {"name": "add_to_cart" | "save_item" | "show_results" | "undo_last" | "start_checkout",
              "look_id"?, "product"? (a Product, as in SearchResponse), "tab"? ("best_match" | "dupes" | "used")}
    answer   {"ok": true} or {"ok": false, "detail": "why, in a few words"}
"""
import json
import logging

from livekit import rtc
from livekit.agents import ToolError, get_job_context

from storefront.rtc.tokens import PHONE, VISION

log = logging.getLogger("storefront.voice")

RPC_TIMEOUT_S = 15.0  # an action can wait on the user (checkout opens Face ID)


async def phone_action(name: str, **args) -> dict:
    """Ask the phone app to do something (its RPC method "action"); its answer. ToolError if it can't."""
    room = get_job_context().room
    phone = next((p for p in room.remote_participants.values() if p.identity.startswith(PHONE)), None)
    if phone is None:
        raise ToolError("The phone app isn't connected.")
    try:
        reply = await room.local_participant.perform_rpc(
            destination_identity=phone.identity, method="action", payload=json.dumps({"name": name, **args}),
            response_timeout=RPC_TIMEOUT_S)
    except rtc.RpcError as e:
        log.warning("voice: action %s failed: %s (%s)", name, e.message, e.code)
        raise ToolError("The phone app couldn't do that.")
    try:
        answer = json.loads(reply) if reply else {}
    except ValueError:
        answer = {}
    if not answer.get("ok", True):
        raise ToolError(answer.get("detail") or "The phone app couldn't do that.")
    log.info("voice: action %s done", name)
    return answer


async def select(uid: str) -> dict:
    """Pick a scene object, as a tap on its hotspot does: the backend's answer, with its Look. ToolError if
    it can't."""
    room = get_job_context().room
    try:
        reply = await room.local_participant.perform_rpc(
            destination_identity=VISION, method="select", payload=json.dumps({"uid": uid}),
            response_timeout=RPC_TIMEOUT_S)
    except rtc.RpcError as e:
        log.warning("voice: select %s failed: %s (%s)", uid, e.message, e.code)
        raise ToolError("The app couldn't look right now.")
    answer = json.loads(reply)
    if not answer.get("ok"):
        raise ToolError(answer.get("detail") or "The app couldn't look right now.")
    return answer
