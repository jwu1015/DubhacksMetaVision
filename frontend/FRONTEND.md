# Spotted: Frontend (iOS Companion App)

> Make any room a storefront. Look at something you like through Meta glasses, and Spotted identifies it and prices it. Then **Claude, acting as your shopping agent**, helps you save it, add it to your cart, find it cheaper or buy it, by voice.

This document covers the **iPhone app** (SwiftUI) and how it talks to the Meta glasses and the FastAPI backend. The backend (image processing, product lookup) is owned by a separate team; see [Backend contract](#backend-contract).

- **Hackathon tracks:** Visa (main) · T-Mobile "Matchmaker" (side)
- **Design file:** [Spotted — iPhone Mockups (Figma)](https://www.figma.com/design/BPa5an0qHEsp2VUNWvKjla)
- **Status:** Design v0.2 complete, implementation not started

---

## Contents
1. [Stack](#stack)
2. [Modes: glasses vs phone](#modes-glasses-vs-phone)
3. [Pipeline](#pipeline)
4. [Claude shopping agent](#claude-shopping-agent)
5. [App architecture](#app-architecture)
6. [Screens](#screens)
7. [Design system](#design-system)
8. [Interaction states](#interaction-states)
9. [Voice & verbal guidelines](#voice--verbal-guidelines)
10. [Accessibility](#accessibility)
11. [Backend contract](#backend-contract)
12. [Setup](#setup)
13. [Build order](#build-order)
14. [Open questions](#open-questions)

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Language / UI | **Swift + SwiftUI** | Native is the right fit for Meta's SDK. No React Native bridge to debug. |
| Target | iOS 17+ (designed for iOS 26 Liquid Glass) | Glasses SDK requires iOS 17+. A physical iPhone is required; there is no simulator support. |
| Glasses | **Meta Wearables Device Access Toolkit (DAT)** | Swift Package: `https://github.com/facebook/meta-wearables-dat-ios`. Start from the sample app in `/samples`. |
| Networking (stream) | **WebRTC** (`stasel/WebRTC` Swift package, prebuilt Google WebRTC) | **Continuous** video track from the glasses to the backend's **WebRTC page**; detections come back over a data channel for live hotspots. |
| Networking (requests) | `URLSession` async/await (HTTPS + JSON) | `/detect`, `/search/{captureID}`, `/checkout`, `/cart` on the FastAPI backend (ngrok HTTPS). Types: backend's `Models.swift`. |
| Speech in | Apple `Speech` framework | Glasses mic arrives over Bluetooth audio. |
| Speech out | `AVSpeechSynthesizer` | Speaks the backend's `quickPhrase`, `spokenSummary` and `Cart.spokenSummary` through the glasses' speakers. |
| Agent | **Claude** (Anthropic API, tool use), called **from the backend** via `POST /agent` (streamed) | Understands requests and acts through tools. The phone executes app-side actions and confirms anything with money. |
| Checkout | **Stripe Checkout (test mode)** in `SFSafariViewController` | The backend returns a checkout URL. Face ID (`LocalAuthentication`) gates opening it. |
| Dev machine | macOS + Xcode | |

> **App Store:** Meta's docs say apps using the DAT SDK can't be published to the App Store yet. Install from Xcode. With a free Apple ID, builds expire after 7 days, so reinstall before the demo.

---

## Modes: glasses vs phone

The app has **two modes and one pipeline**. In both, video goes over **WebRTC** to the backend, which reduces it to **information**: tracked objects with labels, boxes and the best frame per track. What changes is whether the video is **shown**, and how the user picks an item.

| | **Glasses mode** (glasses connected) | **Phone mode** (no glasses) |
|---|---|---|
| Video source | Glasses camera → phone (DAT) → WebRTC | Phone camera (AVFoundation) → WebRTC |
| Shown to the user? | **No.** Processed only; the phone renders no preview. | **Yes.** Live preview, full screen. |
| Detections drive | **Scene memory**: what the user has been looking at | **Hotspots** on the live video (01a) |
| How the user picks | **Voice**: "Spotted, how much is that lamp?" | **Tap** a hotspot (voice also works) |
| Target resolved by | Claude parses the sentence → match against scene memory | The tapped `track_id` |
| Results | **Spoken** in the glasses, plus a capture card, Live Activity and history on the phone | On screen, over the live view (01 / 01b) |

**Switching:** the app picks the mode from the glasses' connection state.
- **Glasses connect** → glasses mode.
- **Glasses disconnect** → phone mode, with a toast: "Glasses disconnected. Using your phone camera."
- **One connection:** the WebRTC peer connection is kept, and the source is swapped with `RTCRtpSender.replaceTrack`, so there's no renegotiation.

### Glasses mode: voice resolved against scene memory

The backend keeps a rolling **scene memory** of the last ~10–15 s. For each `track_id`, it holds the label, box, time in view, how centered it was, last seen, and the **sharpest frame**.

1. **Trigger:** the user starts a voice command (see [Voice trigger](#voice-trigger)), and an earcon confirms it.
2. **Parse:** Claude (`/agent`) turns the sentence into an **action** (`price_check`, `save`, `add_to_cart`, `buy`, `more_info`) and a **target** ("lamp", "the green one", "that").
3. **Resolve the target** against scene memory, with the backend picking a `track_id`:
   - **Label match first,** then rank: in view and centered now > most recently seen > longest looked at.
   - **"That" with no noun** = the most centered object right now.
   - **Two close matches** → ask by voice: "Floor lamp or desk lamp?"
4. **Detect:** `POST /detect` with `track_id` (no upload; the backend uses that track's best buffered frame). This works even if the user already looked away ("the chair I just walked past").
   - Optional upgrade: if the target is still in view, also fire a 12 MP `capturePhoto` for sharper store matching.
5. **Search:** `POST /search/{captureID}` as usual.
6. **Respond:**
   - The glasses speak `quickPhrase`, then `spokenSummary`, and the action is applied ("Saved.").
   - The phone gets a **capture card**: the frame with the item highlighted, so the user can confirm it picked the right thing, plus results and actions.
   - The Live Activity updates, and the item appears in the "Spotted today" feed.
7. **Correction:** "No, the other one" re-resolves to the next-best track.

**Room recap (optional):** when the user leaves a room, the phone shows "You looked at 4 things here", built from scene memory. It's opt-in.

### Phone mode: tap the live video
1. The live preview runs, and the phone camera publishes over WebRTC.
2. `detections` on the data channel → **hotspots** (01a).
3. Tapping a hotspot sends `select` on the data channel, then `POST /detect` with `track_id` (or the frame + `hint_box` as fallback) → brackets → searching card (01b) → results (01).
4. Voice works too: "what's this?" targets the most centered object.

### Voice trigger
The glasses mode depends on this, so options are listed safest first:
1. **In-app push-to-talk:** the mic button, the Live Activity button, or a lock-screen widget. Reliable; **demo default**.
2. **Siri / App Intents:** "Hey Siri, Spotted this" or the iPhone **Action Button**. Hands-free, with no wake-word engine of our own.
3. **Own wake phrase** ("Spotted, …") using on-device Speech while the app is in the foreground. Continuous background listening is limited by iOS and drains the battery.
4. **Glasses tap gesture:** only if Meta's SDK exposes touch events to third-party apps. **Unverified.**

### Privacy (glasses mode streams even when nothing is shown)
- **Always-visible status:** "Glasses camera on · not recording", plus a one-tap **Pause** and the spoken command "pause camera".
- **No storage:** frames live only in the backend's rolling buffer, and nothing is stored unless the user captures it.
- **Captures only on command:** the user can see every capture in the "Spotted today" feed and delete it.

### Battery and heat
- **Glasses mode:** no preview rendering. Drop to ~5 fps and a lower bitrate when the scene is static, and back up to 15 fps when the user starts a voice command.
- **Pause:** the WebRTC video stops entirely when the camera is paused or the app is in the background (unless a capture is in flight).

---

## Pipeline

**One photo becomes a cutout, matched products and a spoken answer.**

```
Meta glasses ──► Phone app ──────────────► FastAPI backend (laptop, exposed via ngrok)
12 MP cam,       DAT SDK sends a JPEG
mics, speakers
                 ┌── POST /detect ─────────────────────────────────────────────────┐
                 │  Claude vision (Anthropic API)  ║  SAM 3 /segment (Modal GPU)   │
                 │  category, description, style,  ║  text prompts from a          │
                 │  brand guess, rough box,        ║  home-goods list → pixel      │
                 │  3 search queries, quick phrase ║  masks + scores               │
                 │  (JSON validated, one retry)    ║  fallback: Roboflow SAM 3,    │
                 │                                 ║  then box crop                │
                 │            └──────► Python match + cutout ◄──────┘             │
                 │            mask overlapping Claude's box → photo crop → URL      │
                 │                         ▼                                       │
                 │  DetectResponse → phone: quick phrase spoken in the glasses,    │
                 │                         /search starts in the background        │
                 └─────────────────────────────────────────────────────────────────┘
                                           │ cutout URL
                 ┌── POST /search/{id} ────▼───────────────────────────────────────┐
                 │  Sources in parallel, ~4 s timeout each:                        │
                 │  Google Lens (exact visual) · Google Shopping (new, top 2       │
                 │  queries) · eBay (used / pre-owned)          — all via SerpAPI  │
                 │                         ▼                                       │
                 │  CLIP /embed (Modal GPU): cutout vs thumbnails, 0–100,          │
                 │  drop < 40                                                      │
                 │                         ▼                                       │
                 │  pricing.py: typical range p25–p75, median → low / fair / high  │
                 │                         ▼                                       │
                 │  choose(): best match, dupes, used; Claude Haiku writes the     │
                 │  spoken summary                                                 │
                 │                         ▼                                       │
                 │  SearchResponse → phone: results + spoken summary in glasses    │
                 └─────────────────────────────────────────────────────────────────┘
                                           │ user picks items
                                           ▼
                        Stripe Checkout (test mode) → checkout URL
```

| Runs on | Steps |
|---|---|
| **Phone** (this repo) | Capture, preview, UI, speech in/out, cart, opening the checkout URL |
| **Laptop** (FastAPI + ngrok) | Orchestration, match + cutout, `pricing.py`, `choose()` |
| **Anthropic API** | Claude vision (`/detect`), Claude Haiku (spoken summary), **Claude shopping agent** (`/agent`) |
| **Modal GPU** | SAM 3 segmentation, CLIP embeddings |
| **External APIs** | SerpAPI (Google Lens, Google Shopping, eBay), Stripe |

### Two layers: continuous stream + per-object pipeline

> Applies to **both modes**. In glasses mode the phone publishes the glasses' video but renders no preview, and selection comes from voice plus scene memory instead of a tap (see [Modes](#modes-glasses-vs-phone)).

```
Glasses ══ continuous video (DAT, 504×896 @ 15 fps) ══► Phone
   Phone ─ live preview (local, never leaves phone)
   Phone ══ WebRTC video track (H.264, ~504×896 @ 15 fps) ══► Backend WebRTC page
                                                                 │ samples frames → lightweight detections
   Phone ◄═══════════ WebRTC data channel "detections" ═══════════┘
                                                                 (boxes + labels → unselected hotspots, 01a)
   On select (tap · gaze dwell ≥ 0.5 s · "what's this?"):
   Phone ── best recent full-res frame ──► POST /detect ──► POST /search/{captureID}  (diagram above)
```

- **Stream layer (always on while Live):**
  - The glasses stream continuously to the phone.
  - The phone republishes the frames as a **WebRTC video track** to the backend's **WebRTC page**, using a custom `RTCVideoCapturer` that feeds each DAT `CMSampleBuffer` in as an `RTCVideoFrame`.
  - The backend samples frames from the track and runs cheap detections (boxes, short labels, scores).
  - It sends them back on a **data channel**; they drive the **hotspots** and keep them tracking objects as the user moves.
  - Bonus: open the WebRTC page on the laptop to show judges the glasses' live view on the projector.
- **Selection layer (per object):** once the user selects an object, the phone runs the full pipeline above on the **sharpest recent frame**. That's either a `capturePhoto` 12 MP still or the best buffered frame cropped around the hotspot's box.
- **Throttling:**
  - Lower the track's frame rate and bitrate (`RTCRtpSender` encoding parameters) while `/detect` and `/search` are running, so the backend's GPU budget goes to the selected object.
  - Back off to 1 fps when nothing changes for several seconds.
  - Stop entirely when the user pauses the camera (privacy).

### What the phone does at each step

1. **Stream:**
   - The DAT session keeps the live preview running.
   - The phone publishes the WebRTC video track to the backend's page and updates hotspots from each `detections` data-channel message.
   - **Selecting** a hotspot, or saying "what's this?", grabs the best full-resolution frame.
2. **`POST /detect`:** upload that frame and show "Looking…" immediately. Send the selected hotspot's box as a hint so Claude and SAM focus on the right object.
3. **On `DetectResponse`:**
   - Speak `quickPhrase` through the glasses ("Lamp. Finding prices."). If `foundSomething` is false, speak it and stop (01c).
   - Place the brackets from `segmentation.bbox` (normalized x/y/w/h).
   - Show the product card in a **searching** state with the cutout crop and `item.category`.
4. **`POST /search/{captureID}`:** call right away (the server is already searching); ~30 s timeout.
5. **On `SearchResponse`:**
   - Fill the card with the best match, price and **price verdict** (low / fair / high).
   - Fill the detail view: Best match / Dupes / Used.
   - Speak `spokenSummary`.
6. **Actions:** Save / Add to cart (client state). **Check out:** send the cart, receive a Stripe URL, confirm with Face ID, and open it in `SFSafariViewController`.

**Latency budget:**
- `/detect`: about 2–3 s (Claude and SAM in parallel). Cover it with the "Looking…" state and an earcon.
- `/detect`: ~1.5 s measured. `/search`: ~4 s today, 8–10 s once real store search lands (use a 30 s timeout). The card shows skeleton rows with "Checking 3 stores…" until it fills.
- The spoken quick phrase lands before search finishes, so the user is never waiting in silence.

---

## Claude shopping agent

Claude is the brain users talk to. The detect/search pipeline **finds** things; the agent **acts** on them: saving, comparing, adding to the cart, and walking the user to checkout. It's also the core of the Visa track story: an AI agent shopping on your behalf, inside a spending limit you set.

### What users can ask

| User says (voice or typed) | Claude does | Glasses say |
|---|---|---|
| "What's this?" | Triggers a scan (`identify_current`) → reads `DetectResponse` | "Lounge chair. $1,299." |
| "Save it for later" | `save_item` | "Saved." |
| "Add it to my cart" | `add_to_cart` | "Chair added. Undo?" |
| "Is that a good price?" | Reads `priceInsight` from `SearchResponse` | "Fair. Typical is $899 to $1,349." |
| "Find it cheaper" / "Any used ones?" | `show_results(tab: dupes / used)` | "Used from $640 on eBay." |
| "Compare the chair and the lamp" | `compare_items` | "Chair's $1,299, lamp's $189." (detail on phone) |
| "Remove the lamp" | `remove_from_cart` (asks first) | "Remove the lamp?" |
| "Buy everything in my cart" | `start_checkout` → **Face ID gate** | "$1,642 to Visa 4242. Confirm on your phone." |
| "What did I save at Maya's?" | `list_saved(room:)` | "Two things: chair and lamp." |

### How it works

```
Glasses mic ─► Phone: Speech → transcript ─► POST /agent (stream) ─► Backend ─► Claude (tools)
                    ▲                                                   │
                    │   events: speech · text · action · confirm · done │
                    └───────────────────────────────────────────────────┘
   Phone executes app-side actions (save, add, open screen) → reports tool_result → Claude continues
```

- **Claude runs on the backend.** The Anthropic API key never ships in the app.
- **Context each turn:** the phone sends a compact snapshot with the current scan (`DetectResponse` summary), the latest `SearchResponse`, the cart, saved items, and the agent limit. Claude only talks about real data and **never invents prices or stock**.
- **Two kinds of tools:**
  - **Server tools**, run by the backend: `identify_current`, `search_more`, `compare_items`, `price_check`.
  - **App tools**, returned to the phone as `action` events and executed locally:
    - `save_item`, `add_to_cart` (→ `POST /cart`), `remove_from_cart` (→ `DELETE /cart/{index}`), `undo_last`
    - `show_results`, `open_screen`, `list_saved`, `start_checkout`

    The phone sends each action's result back so Claude can continue.
- **Streaming:** the backend streams events so the glasses can speak Claude's first short line right away (target: under 1.5 s to first word) while detail fills in on the phone.
- **Model:** use a fast Claude model (e.g., Haiku) for snappy voice turns. Configure it on the backend.
- **Glasses mode makes the agent essential.** Every capture starts as a sentence, so `/agent` must:
  1. parse the **action + target**
  2. call a server tool **`resolve_target(label, qualifiers)`** against scene memory, which returns a `track_id`, or two candidates to ask about
  3. run `/detect` by `track_id`

  In phone mode, the tap already gives the `track_id`, so the agent is only needed for voice requests.

### Guardrails (non-negotiable)

1. **Claude never pays.** `start_checkout` only prepares the order. The phone shows the pay sheet (06) and requires **Face ID or passcode** before calling `POST /checkout`.
2. **Agent limit enforced twice:** on the backend (reject over-limit checkouts) and in the app (06 shows the limit check). Over the limit → "Over your limit. Approve on your phone."
3. **Confirm destructive actions:** removing items, clearing the cart and changing the limit all get a spoken question plus an on-screen confirm.
4. **Undo always works:** save and add are instant but undoable for 10 s ("Chair added. Undo?").
5. **Local fast path:** "stop", "never mind", "cancel" and "undo" are handled **on-device** without waiting for Claude. Speech stops immediately and any pending action is cancelled.
6. **Follows the verbal guide:** the system prompt includes the State script and the 8 voice rules. Spoken lines are ≤ 8 words; longer explanations go to the phone as `text`.
7. **Disclosed:** the UI labels the assistant as AI ("Spotted assistant · Claude"). The glasses don't pretend to be a person.
8. **Camera privacy:** the agent can't turn the camera on by itself; scans only happen on user request or an active Live view.

### Frontend responsibilities
- `AgentClient`: opens `POST /agent`, parses streamed events, and keeps the `session_id`.
- `AgentTools`: executes app tools against `AppState` and returns results.
- `AgentSheet` (02): shows the transcript, Claude's reply, **action cards** ("Added · Walnut Lounge Chair · Undo") and suggested follow-ups.
- `ConfirmGate`: any `confirm` event (checkout, removals) shows a confirm card, with Face ID for money.
- `Speaker`: speaks `speech` events in order and is interruptible by the user ("barge-in").

---

## App architecture

```
Spotted/
├─ App/
│  ├─ SpottedApp.swift          // entry, theme environment
│  └─ AppState.swift            // @Observable: detections, currentProduct, cart, saved, connection
├─ Glasses/
│  └─ GlassesManager.swift      // DAT registration, stream session, frame publisher (lift from Meta sample)
├─ Network/
│  ├─ WebRTCPublisher.swift     // RTCPeerConnection: signaling, video track, "detections" data channel
│  ├─ GlassesVideoCapturer.swift// RTCVideoCapturer: DAT CMSampleBuffer → RTCCVPixelBuffer → RTCVideoFrame
│  ├─ FrameBuffer.swift         // keeps the sharpest recent full-res frame for /detect
│  ├─ APIClient.swift           // POST /detect (multipart JPEG), /search/{captureID}, /checkout, /cart
│  ├─ Config.swift              // BASE_URL = current ngrok https URL
│  └─ Models.swift              // DetectResponse, SearchResponse, Listing, PriceVerdict (Codable)
├─ Agent/
│  ├─ AgentClient.swift         // POST /agent, streamed events (speech, text, action, confirm, done), session_id
│  ├─ AgentTools.swift          // executes app tools: save_item, add_to_cart, remove_from_cart, undo_last,
│  │                            //   show_results, open_screen, list_saved, start_checkout → tool_result
│  ├─ AgentContext.swift        // builds the per-turn snapshot (scan, search, cart, saved, limit)
│  └─ ConfirmGate.swift         // confirm cards + Face ID for anything involving money or removal
├─ Voice/
│  ├─ SpeechInput.swift         // SFSpeechRecognizer → transcript → AgentClient
│  ├─ LocalCommands.swift       // on-device fast path: stop, never mind, cancel, undo
│  └─ Speaker.swift             // AVSpeechSynthesizer + earcons, interruptible (barge-in)
├─ Design/
│  ├─ Tokens.swift              // colors per theme (Lime / Visa Night), type styles
│  └─ Glass.swift               // Liquid Glass helpers / fallbacks
├─ Components/
│  ├─ Hotspot.swift             // unselected marker (default / focused)
│  ├─ DetectionBrackets.swift   // selected marker
│  ├─ ProductCard.swift
│  ├─ GlassTabBar.swift  MicButton.swift  Chip.swift  GlassButton.swift
└─ Screens/
   ├─ LiveView.swift  AgentSheet.swift  ProductDetail.swift
   └─ SavedView.swift  CartView.swift  CheckoutSheet.swift
```

**Data flow:**
- **Continuous:** `GlassesManager` (frames) → `GlassesVideoCapturer` → `WebRTCPublisher` (video track up, data channel down) → `AppState.liveDetections` → hotspots redraw. A copy of each frame goes to `FrameBuffer`.
- **On select:** best frame → `APIClient.detect` → `AppState.current = .detected` → `APIClient.search` → `AppState.current = .matched`. SwiftUI redraws the overlay and card automatically, and `Speaker` voices each phrase.
- **Agent turn:** mic → `SpeechInput` → (`LocalCommands` fast path, or) `AgentClient` with `AgentContext` snapshot → streamed events:
  - `speech` → `Speaker`
  - `text` → `AgentSheet`
  - `action` → `AgentTools` → `AppState` (result back to Claude)
  - `confirm` → `ConfirmGate`

```swift
enum ScanState {
  case idle
  case looking                                  // JPEG uploading / /detect running
  case detected(DetectResponse)                 // quick phrase spoken, card in "searching"
  case matched(DetectResponse, SearchResponse)  // full card + verdict
  case failed(String)
}
```

---

## Screens

Each screen exists in Figma in both themes: **📱 Screens** (Lime) and **📱 Screens · Visa Night**.

The screens below (01–06) are **phone mode** (live video). Screens 03–06 (detail, saved, cart, checkout) are shared by both modes. The glasses-mode screens (G1–G6) are listed after them.

| # | Frame | What it shows |
|---|---|---|
| 01a | Object Detected (Unselected) | The camera feed with a pulsing **Hotspot** on the chair. Its tag appears on hover/focus, and a hint pill reads "Tap the chair or say 'What's this?'". No card yet. |
| 01 | Live View | The chair is selected: lime brackets, a "Lounge Chair · Likely" tag, and a glass product card (Search / Save / Add to cart). The lamp shows as an unselected Hotspot. |
| 02 | Listening → **Agent sheet** | A voice sheet with waveform, live transcript, the line the glasses will say, and suggestion chips. With the agent, this becomes the conversation surface with Claude (see below). |
| 03 | Product Detail | A hero image, a store comparison (lowest price tagged) and a floating glass checkout bar. |
| 04 | Saved | Saved items grouped by room ("Maya's apartment"). Saved = coral + filled bookmark + "Saved". |
| 05 | Cart | Line items, an **Agent limit** card (Visa), the summary, and Check out. |
| 06 | Checkout | A pay sheet showing merchants, card, limit check, the spoken confirmation, and **Pay with Face ID** / passcode. |
| 01b | Searching | After `/detect`: brackets, cutout thumbnail, the spoken quick phrase, skeleton prices and "Checking 3 stores…". Only Save is active. |
| 01c | Couldn't Tell | `/detect` failed: "Couldn't tell what that is", **Try again** / **Take a photo**. |
| 01d | Stream Reconnecting | 01a with a "Reconnecting…" pill and dimmed hotspots (WebRTC dropped). |
| 03b | Dupes & Used | The Used tab with eBay results, and "Google Shopping · Unavailable right now" with Retry. |
| 06b | Stripe Handoff | A Safari sheet with a generic test-mode checkout form (`checkout.stripe.com`). |

**Flow:** 01a → (tap / gaze / "what's this?") → `/detect` → 01 (searching) → `/search` → 01 (matched) → 03 / Save / Add → 05 → 06 → Stripe Checkout.

### Pipeline-driven design updates (now in Figma)
- **01 · Searching state:**
  - The card shows the **cutout** crop, `item.category` and `quickPhrase` (no description; it's empty at detect time).
  - Skeleton price rows read "Checking 3 stores…".
- **01 · Price verdict chip:** "Fair price · typical $899–$1,349" next to the price.
- **03 · Segments:** **Best match / Dupes / Used** replace Stores / Reviews / Similar. Each row shows "Similar" until `matchScore` is live, then "Exact match" at ≥ 85. Dupes show `savingsPct`, any row can show `matchReason`.
- **06 · Checkout:**
  - The final step hands off to **Stripe Checkout (test mode)** in a Safari sheet, gated by Face ID.
  - The Visa card shown is the one the user enters in Stripe (test card `4242…`).
- **Error states:** "Couldn't tell what that is" (detect failed) and per-source "Unavailable right now" (search timeout).

### Agent design updates (not in Figma yet)
- **02 · Agent sheet:**
  - Header "Spotted assistant · Claude" (AI disclosure).
  - The user's transcript, then Claude's reply (short spoken line, plus longer phone-only `text`).
  - **Action cards** for what Claude did ("Saved · Brass Arc Lamp · Undo", "Added · Walnut Lounge Chair · Undo").
  - Suggested follow-ups ("Find it cheaper", "Any used?", "Check out").
- **Confirm card:** for removals and checkout. Shows the question, what will happen, and **Confirm** / **Cancel**, with Face ID for money.
- **Agent working state:** a small "Claude is checking 3 stores…" row while server tools run.
- **06 · Checkout:** add "Prepared by your assistant" plus the request that started it ("Buy everything in my cart"), so the user sees why this sheet appeared.
- **Typed input:** a text field in the Agent sheet for when talking isn't possible.

### Glasses-mode screens (not in Figma yet)
| # | Frame | What it shows |
|---|---|---|
| G1 | Glasses Home | No video. A calm "Listening" state: a glasses-connected pill, "Glasses camera on · not recording" with **Pause**, a big push-to-talk mic, a hint ("Say 'Spotted, how much is that lamp?'"), and the latest captures below. |
| G2 | Voice Command | While the user speaks: waveform, live transcript and the earcon state. |
| G3 | Capture Card | The **frame the backend used, with the item highlighted** (brackets on `segmentation.bbox`), then the searching → matched states (same content as 01b / 01), plus "Not this one" (re-resolve). |
| G5 | Spotted Today | A feed of captures grouped by room: frame thumbnail, item, price, verdict, and Save/Add. It doubles as history and privacy proof ("every capture you asked for"). |
| G6 | Live Activity / Dynamic Island | Compact: "Finding prices · lamp…" → "Lamp · $189 · Fair". Expanded: thumbnail plus Save / Add to cart. Also the lock-screen push-to-talk entry. |
| G7 | Room Recap (optional) | "You looked at 4 things at Maya's", with Save all / pick. |

**Mode switch toast:** "Glasses disconnected. Using your phone camera." / "Glasses connected. Say 'Spotted' to capture."

**Not yet designed:** onboarding (pairing, permissions, linking a Visa card, setting the limit), the receipt, an over-limit approval sheet, empty states, and anything for the T-Mobile Matchmaker side track.

---

## Design system

The Figma source of truth is the **"Spotted Color"** variable collection, which has two modes: **Dark (Lime)** and **Visa Night**.

### Color tokens

| Token | Lime (default) | Visa Night | Use |
|---|---|---|---|
| `bg/ink` | `#0A0A0C` | `#070B24` | App background |
| `bg/surface` | `#16161A` | `#0F1638` | Cards, lists |
| `bg/surface-raised` | `#222228` | `#18214D` | Thumbnails, wells |
| `separator` | `#2C2C32` | `#222B57` | Hairlines |
| `label/primary` | `#FFFFFF` | `#FFFFFF` | Titles, body |
| `label/secondary` | `#A1A1AA` | `#AAB3D6` | Supporting text |
| `label/tertiary` | `#8A8A94`* | `#8892BD` | Meta text |
| `on-accent` | `#0A0A0C` | `#0A0F2E` | Text on accent fills |
| `accent/lime` | `#C8FF3D` | `#F7B600` | Primary action, selected brackets, brand |
| `accent/lime-pressed` | `#A8DB1F` | `#D99F00` | Pressed primary |
| `accent/coral` | `#FF7A59` | `#FF8C7A` | Saved (always with icon + label) |
| `brand/visa-blue` | `#1434CB` | `#1434CB` | **Payment moments only** |
| `status/success` | `#30D158` | `#4ADE9B` | Within limit, paid |
| `status/warning` | `#FFD60A` | `#FF9F43` | Reconnecting, offline |
| `status/error` | `#FF6961`* | `#FF7A85` | Declined, failed |
| `glass/label` | `#16161A` @ 72% | `#0F1638` @ 72% | Small glass labels (alpha lives in the token) |

\* Accessibility fixes, now synced to Figma: tertiary `#6B6B75 → #8A8A94` and error `#FF453A → #FF6961`. Visa Night warning moved to orange `#FF9F43` so it doesn't read as the gold accent.

### Typography

The mockups use **Inter**, a stand-in for SF Pro, which doesn't render in Figma's cloud renderer. **In code, use the SwiftUI system text styles** so you get SF Pro and Dynamic Type for free.

| Figma style | Size / line | SwiftUI |
|---|---|---|
| Large Title | 34/41 Bold | `.largeTitle` |
| Title 1 | 28/34 Bold | `.title` |
| Title 2 | 22/28 Bold | `.title2` |
| Headline / Button | 17/22 Semibold | `.headline` |
| Body | 17/22 Regular | `.body` |
| Callout | 16/21 Medium | `.callout` |
| Subhead | 15/20 Regular | `.subheadline` |
| Footnote | 13/18 Regular | `.footnote` |
| Caption | 12/16 Medium | `.caption` |
| Price | 34/40 Bold | `.largeTitle.monospacedDigit()` |
| Transcript | 22/28 Medium | `.title3.weight(.medium)` |
| Wordmark | Space Grotesk Bold | Brand only (custom font) |

### Liquid Glass

| Material | Spec | Use |
|---|---|---|
| **Regular** | Glass + `bg/surface` tint at 55–78% + 1pt inside stroke (white at 18%) | Anything with text over the camera feed |
| **Clear** | Low tint, more refraction | Icon-only controls over dark areas; never body text |
| **Tinted accent** | Solid accent fill + `on-accent` text | Primary action only. Never accent-colored text on glass |

- **iOS 26:** use the system Liquid Glass (`.glassEffect()` / `GlassEffectContainer`, and the system tab bar's floating style).
- **iOS 17–18 fallback:** `.ultraThinMaterial` + dark tint overlay + hairline stroke.
- **Layout rules:**
  - Tap targets ≥ 44pt; mic button 62pt, 8pt from the tab bar.
  - Safe areas: 59pt top / 34pt bottom; 12–16pt side margins.
  - Radii: controls 22–31, cards 24–32, sheets 40.

### Components (🧩 Components page)

| Component | Notes |
|---|---|
| **Hotspot** | Variants `State=Default / Hover`; text prop `Item`. Unselected marker. |
| **Detection Brackets** | Selected marker; accent with a soft glow. |
| **Tab Bar** | Variants `Selected=Live / Saved / Cart`; floating glass. |
| **Mic Button** | 62pt glass circle, push-to-talk. |
| **Status Bar, Home Indicator, Camera Feed, Product Art** | Mockup scaffolding only. |

---

## Interaction states

| State | Marker | Card | Trigger to next |
|---|---|---|---|
| Unselected (01a) | **Hotspot**: pulsing dot, no tag | none | tap / focus / "What's this?" |
| Focused / hover | Hotspot brightens; **item tag appears** ("Lounge chair · Tap to view") | none | tap or confirm |
| Selected (01) | **Accent brackets** + "Name · Likely" tag | Product card | Save / Add / Search |

**Hover on iPhone:** there's no pointer hover on touch. Map the Figma Hover state to:
1. **Glasses gaze:** the user's view stays centered on the object for about 0.5 s (preferred).
2. **Press-and-hold** on the hotspot (phone fallback).
3. VoiceOver focus: the tag's text is the hotspot's accessibility label.

Transitions: 200 ms ease-out (matches the Figma Smart Animate). Respect **Reduce Motion**: no pulse, instant swap.

---

## Voice & verbal guidelines

The full guide is on the Figma **🎨 Style Guide** page (Verbal). Core rules:

1. **Answer first:** name, then price.
2. **≤ 8 words per spoken turn;** detail goes to the phone.
3. **Say what you heard:** echo the action.
4. **Unsure? Offer two;** never guess silently.
5. **Money is explicit:** amount + card + "confirm", always confirmed with Face ID or passcode on the phone. **Speech alone never pays.**
6. **Calm, not salesy:** no hype, no "!".
7. **Always an exit:** "stop", "never mind" and "undo" work everywhere and interrupt speech.
8. **Privacy out loud:** announce when the camera pauses.

**For Claude as the agent:** these rules (and the State script) go into the agent's system prompt. Claude speaks in the product's voice ("Saved.", not "I have saved that item for you."), but the UI discloses that the assistant is AI. When Claude takes an action, it says the result, not the process.

### State script (single source of truth for spoken lines)

| State | Glasses say | Phone shows | Earcon |
|---|---|---|---|
| Identified | "Lounge chair. $1,299." | Product card (01) | Two-note chime |
| Added | "Chair added. Undo?" | Cart +1 | Coin clack |
| Saved | "Saved." | Coral bookmark + "Saved" | Soft whoosh |
| Over limit | "Over your limit. Approve on your phone." | Limit approval sheet | Low double tone |
| Checkout | "$1,642 to Visa 4242. Confirm on your phone." | Pay sheet (06) | Heartbeat |
| Paid | "Done. Receipt's on your phone." | Receipt | Success chord |
| Disconnected | Phone speaks: "Glasses disconnected." | Toast; switches to the phone camera | Falling tone |
| Camera paused | "Camera paused." | Dimmed feed | Click |

**Vocabulary:** Spot / spotted · Save (not "favorite") · Add to cart · Check out (only at payment) · Room · Agent limit.

---

## Accessibility

Target: **WCAG 2.2 AA** (the standard that the ADA, Section 508 and the European Accessibility Act reference).

- **Text contrast ≥ 4.5:1.** All tokens pass after the two fixes above (lowest pair 5.3:1).
- **Non-text contrast ≥ 3:1** for brackets, hotspots, button edges and focus rings.
  - **Camera feed caveat:** every accent drops to about 1.1–1.3:1 over bright areas such as windows. Give on-feed markers a dark outline or shadow, and keep all on-feed text on Regular glass.
  - Visa blue on dark backgrounds is about 2.2:1, so Visa marks need a light outline.
- **Never color alone:** Saved = coral + filled icon + label; selected tab = accent + glass pill; confidence is shown as text ("Likely").
- **Dynamic Type:** use system text styles; test cards at the largest accessibility sizes.
- **VoiceOver:** hotspots are buttons labeled with the item name; the product card reads name, price and actions in order.
- **Reduce Motion:** disable the hotspot pulse and bracket lock-in animation.

---

## Backend contract

Base URL: the current **ngrok HTTPS** URL, set in `Config.swift`. It changes each time ngrok restarts unless the team uses a reserved domain.

> **Source of truth: `Models.swift`** from the backend team. It matches `backend/models.py` exactly, decodes 14 real server responses (including null cases and error bodies), and compiles in Swift 5 and 6. Drop it into `Network/Models.swift` as-is.
> - Decode with a plain `JSONDecoder()` (all keys are mapped in `CodingKeys`; don't set `keyDecodingStrategy`).
> - Optionals really can be `nil` at runtime. Never force-unwrap them.
> - Errors (any non-2xx) decode as `APIError { detail }`.

| Endpoint | Request | Response | Status |
|---|---|---|---|
| `POST /detect` | multipart `image` (JPEG, quality ~0.8, long side ≤ 2048 px); soon optional `hint_box`; **requested:** `track_id` (use buffered frame), `hint_text`, `hint_point` | `DetectResponse` | Live (~1.5 s); new fields requested |
| `POST /search/{captureID}` | no body | `SearchResponse` (404 if unknown id) | Live (~4 s now, 8–10 s with real store search) |
| `POST /checkout` | `CheckoutRequest` | `CheckoutResponse` | Live (test "Order placed!" page; Stripe test mode coming) |
| `POST /cart` · `GET /cart` · `DELETE /cart/{index}` | `AddToCartRequest` | `Cart` (with `spokenSummary`) | Coming with the search/cart PR |
| `POST /offer` + data channel `detections` | `SessionDescription` | `SessionDescription`, `DetectionsMessage` | Our format, adopted by the backend; not live yet |

### WebRTC page (continuous stream)
The backend hosts a **WebRTC page**; the phone is a publisher.

**Signaling** (confirm with backend; this is the common aiortc pattern):
```jsonc
// phone → backend:  POST /offer   (over the ngrok HTTPS URL)
{ "sdp": "<phone SDP offer: 1 video track (sendonly) + data channel 'detections'>", "type": "offer" }
// backend → phone
{ "sdp": "<SDP answer>", "type": "answer" }
```

**Media:** video track, H.264 (VP8 as fallback), about 504×896 at 15 fps. The phone sends only; nothing comes back on the video track.

**Data channel `detections`** (backend → phone, a few times per second):
```jsonc
{ "type": "detections", "ts": 1739812345123,
  "objects": [ { "track_id": "t3", "box": [0.31,0.22,0.58,0.71], "label": "chair", "score": 0.88 },
               { "track_id": "t4", "box": [0.70,0.30,0.98,0.60], "label": "lamp",  "score": 0.74 } ] }
```
- **Backend options:** SAM 3 text prompts on sampled frames, or a lighter detector, with simple tracking (`track_id`) so hotspots don't flicker.
- **Message types:** use `ts` to ignore out-of-order messages. Optionally the phone can send `{"type":"select","track_id":"t3"}` on the same channel so the backend knows what's selected.

> **ngrok gotcha:** ngrok only tunnels the HTTP **signaling**. WebRTC **media goes over UDP**, peer to peer, and doesn't pass through ngrok.
> - **Same Wi-Fi or hotspot** as the laptop: works with host ICE candidates and a public STUN server (`stun:stun.l.google.com:19302`).
> - **Different networks or a strict venue Wi-Fi:** you need a **TURN** server (e.g., Cloudflare or Twilio TURN), configured on both peers.
> - Demo safest bet: phone and laptop on the same phone hotspot.

### `POST /detect` → `DetectResponse`
Request: `multipart/form-data`, field `image` (JPEG, quality ~0.8, long side ≤ 2048 px). Coming soon: optional `hint_box` form field, the tapped hotspot's box as JSON `{"x","y","w","h"}` (use `BBox(corners: detection.box)`), so the backend cuts out the object the user chose.

| Field | Use in the app |
|---|---|
| `captureID` | Pass to `/search/{captureID}`; ignore any response whose `captureID` isn't the current scan |
| `quickPhrase` | **Speak immediately** ("Lamp. Finding prices.") |
| `foundSomething` (computed) | `false` → speak `quickPhrase` ("I couldn't spot a product there…"), show 01c, **stop** |
| `segmentation.bbox` | SAM's tight box, **normalized `{x, y, w, h}`** → `bbox.rect(in: viewSize)` for brackets |
| `segmentation.cutoutURL` | Thumbnail on the searching card. It's a **crop of the photo**, not the item on white. |
| `segmentation.maskScore` | SAM confidence; `nil` on fallback crop |
| `item.category` | Card title while searching ("Lamp") |
| `item.description / style / brandGuess / searchQueries` | **Usually empty at `/detect` time** (SAM answers first; Claude describes the item server-side). Don't show them from `/detect`. |

### `POST /search/{captureID}` → `SearchResponse`
The server already started searching during `/detect`, so this call just waits for the result. Use a **~30 s request timeout** and keep the card in the searching state (01b) meanwhile.

| Field | Use in the app |
|---|---|
| `bestMatch: Product?` | Main card. Can be **nil** (nothing for sale), so show "No exact match for sale" and fall back to dupes/used. |
| `dupes: [Product]` | Dupes tab; cheapest savings first. Can be empty. |
| `used: [Product]` | Used tab. Can be empty. |
| `priceInsight.verdictText` | Ready-made line for the price verdict block ("West Elm is above the typical $120–$329 range.") |
| `priceInsight.bestMatchVerdict.label` | Verdict chip: "Low price" / "Fair price" / "High price" |
| `priceInsight.typicalLow / typicalHigh / median` | Range bar (p25 / p75 / median). Can be **nil**, so hide the bar. |
| `spokenSummary` | **Speak when the cards appear** |
| `sourcesOK` | Not sent yet, so it decodes as `nil`. **Treat `nil` as "all stores fine".** |

`Product` fields that differ from the design's earlier names:
- **Image:** `imageURL` (optional), not `thumbnail`.
- **Similarity:** `matchScore` (optional, 0–100), not `similarity`. It's **`nil` until CLIP scoring is live**, so show **"Similar"**, never "Exact match", until `isExactMatch` can be true.
- **Tier:** `tier` is `new` / `dupe` / `used`, and `tier.condition` gives "new" / "pre-owned".
- **Source:** `source` is `lens` / `shopping` / `ebay`, and `source.displayName` gives "Google Lens" and so on.
- **New fields:** `savingsPct` (dupes: "Save 34%") and `matchReason` (e.g. "Same tripod base and drum shade"), both great as a caption.
- **Buy:** opens `product.url`.

### `POST /agent`  (streamed, Server-Sent Events)
```jsonc
// phone → backend
{
  "session_id": "s_91c2",                  // reuse across turns; null to start
  "message": "add it to my cart and save the lamp",   // transcript or typed text
  "context": {                             // built by AgentContext each turn
    "current_scan": { "id": "det_7f3a", "category": "lounge chair", "best_match": { "title": "Walnut Lounge Chair", "price": 1299.00, "store": "Article" }, "verdict": "fair" },
    "mode": "glasses",                     // "glasses" | "phone"
    "visible_objects": [ { "track_id": "t4", "label": "lamp" } ],
    "recent_objects": [                    // glasses mode: scene memory summary (requested from backend)
      { "track_id": "t4", "label": "lamp", "in_view": true, "centered": 0.82, "seconds_ago": 0, "seconds_viewed": 3.4 },
      { "track_id": "t7", "label": "chair", "in_view": false, "centered": 0.40, "seconds_ago": 6, "seconds_viewed": 2.1 } ],
    "cart":  [ { "id": "c1", "title": "Brass Arc Lamp", "price": 189.00 } ],
    "saved": [ { "id": "s1", "title": "Ceramic Bud Vase", "room": "Blue Bottle Coffee" } ],
    "agent_limit": { "amount": 2000.00, "currency": "USD", "card_last4": "4242" }
  },
  "tool_results": []                       // results of app tools from the previous turn, if any
}
```
```jsonc
// backend → phone: event stream
event: speech   data: { "text": "Chair added." }                                    // speak now (≤ 8 words)
event: action   data: { "tool_use_id": "tu_1", "name": "add_to_cart", "input": { "scan_id": "det_7f3a" } }
event: action   data: { "tool_use_id": "tu_2", "name": "save_item",   "input": { "track_id": "t4" } }
event: text     data: { "text": "Added the Walnut Lounge Chair ($1,299, fair price) and saved the lamp." }
event: speech   data: { "text": "Lamp saved. Undo?" }
event: done     data: { "session_id": "s_91c2", "awaiting_tool_results": ["tu_1", "tu_2"] }
```
- Then the phone runs the actions, updates the UI and POSTs `tool_results` (`[{ "tool_use_id": "tu_1", "ok": true }]`) so Claude can continue if needed.
- **Confirm event:** for checkout and removals. The phone must show `ConfirmGate`, and only after the user confirms (Face ID for money) does it call `/checkout` or execute the removal:
  ```jsonc
  event: confirm  data: { "tool_use_id": "tu_3", "kind": "checkout", "summary": "2 items · $1,642.01 · Visa 4242", "within_limit": true }
  ```

### `POST /checkout` → `CheckoutResponse`
Send `CheckoutRequest(items: cart.items.map(CheckoutItem.init))`, then open `checkoutURL` in `SFSafariViewController` (after the Face ID gate). Today it returns a test "Order placed!" page; Stripe test mode is coming.

### Cart (server-side, coming with the search/cart PR)
- `POST /cart` with `AddToCartRequest(product:)`: send a `Product` exactly as `/search` returned it.
- `GET /cart` and `DELETE /cart/{index}`.
- Each returns `Cart { items, total, spokenSummary }`. Speak `spokenSummary` after adds and removes.
- Until the PR lands, keep the cart in `AppState` and switch to the server cart when the endpoints are live.

### UI rules
- **Hotspots from the stream:**
  - Each `objects[]` entry with `score ≥ 0.6` becomes an **unselected Hotspot** (01a) at the center of its `box`.
  - Key hotspots by `track_id` and animate them to new positions. Fade one out after ~1 s without updates.
  - While the data channel is disconnected, show the stream as "Reconnecting…" in the status pill.
- **Selected:** tapping a hotspot, gaze dwell or "what's this?" makes that object **selected** (brackets from `segmentation.bbox`) and starts `/detect`.
- **Nothing found:** `foundSomething == false` → speak `quickPhrase`, show **Couldn't tell** (01c), and don't call `/search`.
- **Similarity:** show "Exact match" only when `product.isExactMatch` (`matchScore ≥ 85`). While `matchScore` is `nil` (today), show **"Similar"**.
- **Price verdict chip:** text is `bestMatchVerdict.label`. Color: Low = success, Fair = secondary, High = warning. Always text plus color.
- **Empty states:** `bestMatch == nil` → "No exact match for sale"; empty `dupes` or `used` → "No dupes found" / "No used listings".
- **Partial results:** only when `sourcesOK` is non-nil and missing a source, show that source as "Unavailable right now". `nil` means all fine.
- **Stale responses:** ignore any response whose `captureID` isn't the current scan.
- **Errors:** show `APIError.detail` only in debug builds. Users see the verbal-guide line ("Couldn't reach the store search. Try again.").

---

## Setup

1. Install Xcode on the Mac. Pair the glasses in the **Meta AI** app.
2. Clone `meta-wearables-dat-ios` and run the **sample app** on your iPhone. Seeing the glasses' camera on the phone de-risks the hardest part.
3. In this project: **File → Add Package Dependencies →** the repo above. Then `import MWDATCore` and `import MWDATCamera`.
4. Info.plist entries (copy from the sample):
   - `CFBundleURLTypes`: your URL scheme for Meta AI callbacks
   - `MWDAT` dictionary: `AppLinkURLScheme`, `MetaAppID`, `ClientToken`, `TeamID` (app ID and token not needed in Developer Mode)
   - `UIBackgroundModes` → `bluetooth-central`, plus `NSBluetoothAlwaysUsageDescription`
   - `NSLocalNetworkUsageDescription` + `NSBonjourServices`: as required by the DAT SDK for Wi-Fi streaming
   - `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`, `NSSpeechRecognitionUsageDescription`, `NSFaceIDUsageDescription`
5. **Base URL (secret):** the backend team shares it by DM. **Never commit it.** The repo is public, and every `/detect` costs a Claude call.
   - Put it in an **untracked** `Config.xcconfig` (`API_BASE_URL = https:/$()/…`) read via Info.plist, or an untracked `Config.swift`.
   - Add the file to `.gitignore` and commit a `Config.example.xcconfig` with a placeholder.
   - HTTPS means no App Transport Security exceptions. No ngrok header is needed; `URLSession` requests (API and images) go straight through.

---

## Build order

- [ ] 1. Meta sample app running with the glasses' camera visible on the phone
- [ ] 2. Own app with the live preview + `AppState`
- [ ] 3. Drop in the backend's `Models.swift`, then `APIClient` (`/detect`, `/search`, `/checkout`) against the real backend
- [ ] 4a. WebRTC: add `stasel/WebRTC`, publish a test track (phone camera) to the backend page via `POST /offer`, and confirm it plays on the laptop
- [ ] 4b. Swap in `GlassesVideoCapturer` (DAT frames → track); handle `detections` → live hotspots
- [ ] 4c. Select a hotspot → best full-res frame → `/detect` → speak `quickPhrase` → card in searching state (01b)
- [ ] 5. `/search/{captureID}` → fill card: `bestMatch`, verdict chip, `verdictText`; speak `spokenSummary`; handle nil/empty
- [ ] 6. Hotspot (01a) → selection → brackets
- [ ] 7. Product Detail (03): Best match / Dupes / Used
- [ ] 8. Saved (04) and Cart (05)
- [ ] 9. Checkout (06): Face ID → `POST /checkout` → Stripe URL in `SFSafariViewController`
- [ ] 10. Voice in (Speech) → transcript; `LocalCommands` fast path (stop / never mind / undo)
- [ ] 10a. `AgentClient` against a mock `/agent` SSE stream; `AgentTools` for save / add / remove / undo
- [ ] 10b. Agent sheet (02) with action cards + typed input; `ConfirmGate` for removals and checkout (Face ID)
- [ ] 10c. Point at the real `/agent`; test the guardrails (over-limit, removal confirm, barge-in)
- [ ] 11. Error / timeout states
- [ ] 12. Point at the real ngrok backend
- [ ] 13. Demo polish: pre-staged room with 3 products that segment and match reliably

**Two-mode pivot (added Oct 17):**
- [ ] M1. `CaptureMode` (glasses / phone) in `AppState`, switched from the glasses' connection state, plus the toast
- [ ] M2. Phone camera source (AVFoundation) → preview + WebRTC track; `replaceTrack` on mode switch
- [ ] M3. Glasses mode: publish the DAT track with **no preview**; status pill + Pause
- [ ] M4. Push-to-talk → `SpeechInput` → `/agent` (action + target) → `track_id` → `/detect` by `track_id` → `/search`
- [ ] M5. Capture card (G3) with highlighted frame; "Not this one" → next track
- [ ] M6. Spotted Today feed (G5)
- [ ] M7. Live Activity (G6) via ActivityKit
- [ ] M9. Stretch: Siri / App Intents + Action Button trigger; room recap (G7)

---

## Open questions

- **Scene memory:** how long a window (10–15 s?), and does the backend keep the best frame per track, or only boxes?
- **`/detect` by `track_id`:** can the backend run it on its buffered frame? This makes glasses mode much faster and keeps it consistent with what was seen.
- **Voice trigger for the demo:** push-to-talk (default), Siri / Action Button, or a foreground wake phrase?
- **Room recap:** in or out for the demo?

- **Theme:** Lime (default) or Visa Night for the demo? Both are ready in Figma.
- **Agent scope:** which tools ship for the demo? Must-haves: `save_item`, `add_to_cart`, `undo_last`, `show_results`, `start_checkout`. Stretch: `compare_items`, `list_saved(room:)`.
- **Agent memory:** does the session persist across app launches (e.g., "what did I save yesterday?"), or only within one session?
- **Assistant name:** "Spotted assistant" (powered by Claude) or a named persona? Keep the disclosure either way.
- **Phone "hover":** gaze vs press-and-hold for revealing the item tag.
- **Visa track story:** checkout runs on **Stripe test mode**. Decide how Visa shows up for judges: Visa test cards in Stripe, the "agent limit" concept, or a Visa sandbox API (Intelligent Commerce / Token Service) layered on. Ask the Visa mentors early.
- **`/agent` owner:** not on the backend yet. Decide who builds it, or fall back to on-device keyword commands (see the agent section).
- **WebRTC page details:** confirm with the backend team:
  - the signaling route (`POST /offer`?) and SDP format
  - codec (H.264 vs VP8)
  - the data channel name and message shape
  - which model samples the stream, and at what rate
  - whether a TURN server is needed at the venue

  Fallback if WebRTC isn't ready: send a frame to `/detect` every ~2 s.
- **T-Mobile Matchmaker:** get the brief from organizers. Ideas: nearest store with stock, "who else would love this?", 5G as the "instant" story.
- **Glasses hardware:** Ray-Ban Display (in-lens screen, gated preview) or audio-only Ray-Ban Meta?
