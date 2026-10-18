"""The voice agent asks the phone app to act, through the phone's RPC method "action":

    payload  {"name": "add_to_cart" | "save_item" | "show_results" | "undo_last" | "start_checkout",
              "look_id"?, "product"? (a Product, as in SearchResponse), "tab"? ("best_match" | "dupes" | "used")}
    answer   {"ok": true} or {"ok": false, "detail": "why, in a few words"}
"""
import json
import logging

from livekit import rtc
from livekit.agents import ToolError, get_job_context

log = logging.getLogger("storefront.voice")

RPC_TIMEOUT_S = 15.0  # an action can wait on the user (checkout opens Face ID)


async def phone_action(name: str, **args) -> dict:
    """Ask the phone app to do something (its RPC method "action"); its answer. ToolError if it can't."""
    room = get_job_context().room
    phone = next((p for p in room.remote_participants.values()
                  if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD), None)
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
