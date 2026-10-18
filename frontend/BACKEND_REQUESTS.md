# Backend requests: two-mode pivot

**From:** Frontend · **Date:** Oct 17 · **Context:** `frontend/FRONTEND.md` → "Modes: glasses vs phone"

## The pivot
WebRTC stays on in **both modes** and keeps reducing video to information (tracks, labels, boxes). The only difference is presentation:

- **Glasses mode:** the glasses' video streams but is **not shown** to the user. The user picks items **by voice** ("Spotted, how much is that lamp?"), and the target is resolved against what the user has recently been looking at (**scene memory**).
- **Phone mode:** the phone camera's video is shown live, and the user **taps hotspots** (exactly today's plan).

Everything after `/detect` (search, cart, checkout) is unchanged.

---

## Requests, in priority order

| # | Request | Priority | Frontend fallback if not ready |
|---|---|---|---|
| R1 | Scene memory per WebRTC session | **P0** | Phone keeps the last ~15 s of `detections` and ranks on-device |
| R2 | `/detect` by `track_id` (use the buffered best frame) | **P0** | Phone uploads its own frame + `hint_box` |
| R3 | Richer `detections` messages (in view, centered, dwell) | **P0** | Phone computes centered/dwell from boxes over time |
| R4 | `/detect` hints: `hint_text`, `hint_point`, `hint_box` | P1 | Crop on the phone before upload |
| R5 | Cut-down `POST /agent` with `resolve_target` | P1 | On-device keyword parsing + ranking |
| R6 | Data-channel control messages: `mode`, `pause`, `select` | P1 | Pause = stop sending video |
| R7 | Privacy guarantees for the rolling buffer | P1 | n/a; we need this for the pitch |
| R8 | `/health` additions | P2 | Hide the indicators |

---

### R1 · Scene memory (P0)
Per WebRTC session, keep a rolling window (proposal: **15 s**) of tracks:

```jsonc
{ "track_id": "t4", "label": "lamp", "score": 0.81,
  "first_seen_ts": 1739812331000, "last_seen_ts": 1739812345123,
  "in_view": true,
  "centered": 0.82,          // 0–1, how close to frame center (time-weighted)
  "seconds_viewed": 3.4,     // total time in view within the window
  "best_frame_id": "f_118"   // sharpest frame where this track is visible
}
```
- Keep the **best frame per track** (sharpness × box size × centeredness), not every frame.
- Drop tracks after the window expires.
- Questions: is 15 s workable for memory and GPU? Can the tracker keep `track_id` stable across brief occlusions (~1 s)?

### R2 · `/detect` by `track_id` (P0)
```
POST /detect   multipart: track_id=t4   (no image)
```
- Run the existing detect pipeline on that track's **best buffered frame**, using the track's box as the hint.
- **Response:** the same `DetectResponse`. Please make `imageURL` the **frame that was used**, because the phone shows it on the capture card with `segmentation.bbox` highlighted.
- **Errors:** `404 {"detail": "track expired"}` when the track has left the window. The phone then falls back to uploading a frame.
- This removes an upload and guarantees the photo matches what the user was looking at.

### R3 · Richer `detections` messages (P0)
Add optional fields to each object in the existing `detections` data-channel message (backwards-compatible):

```jsonc
{ "type": "detections", "ts": 1739812345123,
  "objects": [ { "track_id": "t4", "box": [0.70,0.30,0.98,0.60], "label": "lamp", "score": 0.74,
                 "in_view": true, "centered": 0.82, "seconds_viewed": 3.4, "last_seen_ts": 1739812345123 } ] }
```
- **Phone mode:** these drive hotspots (as today).
- **Glasses mode:** the phone uses them for the agent context and as a fallback for resolving targets on-device.
- Also include **recently out-of-view** tracks (`in_view: false`) for a few seconds, so "the chair I just passed" works.

### R4 · `/detect` hints (P1)
Optional multipart fields, used when the phone uploads an image (phone mode, or a fallback):
- `hint_box`: `{"x","y","w","h"}` normalized (already planned).
- `hint_point`: `{"x","y"}` normalized, the tapped point. Use it as a SAM point prompt.
- `hint_text`: `"green floor lamp"` from the voice command. Use it as the SAM text prompt and pass it to Claude.

### R5 · Cut-down `POST /agent` (P1)
Glasses mode needs it: every capture starts as a sentence. Non-streaming is fine for the demo.

```jsonc
// request
{ "session_id": "s_91c2", "message": "how much is that lamp",
  "mode": "glasses",
  "context": { "cart_count": 1, "agent_limit": { "amount": 2000, "card_last4": "4242" },
               "current_capture_id": null } }
// response
{ "speech": "Checking the lamp.",
  "action": "price_check",                 // price_check | save | add_to_cart | buy | more_info | none
  "target": { "label": "lamp", "qualifiers": ["floor"] },
  "resolved": { "track_id": "t4" },        // or null
  "ask": null }                             // or { "question": "Floor lamp or desk lamp?", "options": [ {"track_id":"t4","label":"floor lamp"}, {"track_id":"t9","label":"desk lamp"} ] }
```
- **Server tool `resolve_target(label, qualifiers)`:** match labels in scene memory, then rank in view and centered now > most recently seen > longest viewed. "That" with no noun means the most centered object in view.
- Return `ask` when the top two candidates are close, rather than guessing (that's the verbal guide's "unsure? offer two" rule).
- **The phone does the rest:** `/detect` by `track_id` → `/search` → apply the action. Claude never pays; checkout stays behind Face ID on the phone.
- **Ownership:** who can own this? About 2 h given the existing Anthropic client.

### R6 · Data-channel control messages (P1)
Phone → backend, on the existing `detections` channel:
```jsonc
{ "type": "mode",  "mode": "glasses" }     // or "phone"; sent on connect and on every switch
{ "type": "pause", "paused": true }         // camera paused by the user
{ "type": "select", "track_id": "t4" }      // already proposed: user tapped a hotspot
```
- `mode` lets the backend tune itself, for example by keeping scene memory longer in glasses mode.
- The phone swaps the video source with `RTCRtpSender.replaceTrack` (glasses ↔ phone camera) **without renegotiating**. Please make sure tracking tolerates a sudden source change: reset tracks when you receive a `mode` message.
- The phone also lowers the frame rate to ~5 fps when the scene is idle and raises it to 15 fps during a voice command. Please make sure the tracker handles a variable fps.

### R7 · Privacy (P1)
The glasses stream continuously while showing nothing, so we need to be able to state:
- Frames exist only in the rolling buffer (R1) and are **never written to disk** unless a capture happens (`/detect`).
- The buffer and tracks are **purged** on `pause`, disconnect or session end.
- Captured frames (`imageURL`) are kept only for the session, or for a fixed time you choose.

### R8 · `/health` additions (P2)
`webrtc_sessions: Int`, `scene_memory: Bool` (R1 live), `agent: Bool` (R5 live). The phone shows these in debug builds.

---

## No changes needed
- `/search/{captureID}`, `/checkout`, `/cart*`, `Models.swift` types, and the `detections` base format all stay as they are.
- The base URL stays secret; the frontend keeps it in an untracked config.

## Timeline ask
If R1–R3 land first, glasses mode works end to end with on-device resolution. R5 makes it smart. Please reply with what's feasible before the demo and who owns each item.
