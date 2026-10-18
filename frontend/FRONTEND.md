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
| Networking (live) | **LiveKit** (`client-sdk-swift`) | One room per session with the backend in it: the camera goes up as one video track, objects, labels and Looks come back on topic `updates`, and taps go out as RPCs. |
| Networking (requests) | `URLSession` async/await (HTTPS + JSON) | `POST /session/token` on the FastAPI backend (ngrok HTTPS). Types: `Models.swift`, mirroring `backend/storefront/models.py`. |
| Voice | The voice agent in the LiveKit room | The phone's mic goes up as an audio track and the agent answers over the room. In glasses mode (`GlassesAudio`) the glasses' mic and speakers carry it over HFP, set up before the glasses stream starts (Meta's documented order); if HFP doesn't come up, the phone's mic and speaker. No speech on the device. |
| Agent | **Claude** (Anthropic API, tool use), called **from the backend** via `POST /agent` (streamed) | Understands requests and acts through tools. The phone executes app-side actions and confirms anything with money. |
| Checkout | **Stripe Checkout (test mode)** in `SFSafariViewController` | The backend returns a checkout URL. Face ID (`LocalAuthentication`) gates opening it. |
| Dev machine | macOS + Xcode | |

> **App Store:** Meta's docs say apps using the DAT SDK can't be published to the App Store yet. Install from Xcode. With a free Apple ID, builds expire after 7 days, so reinstall before the demo.

---

## Modes: glasses vs phone

The app has **two camera sources and one pipeline**. The user picks the source on the start screen; the glasses can only be picked while they're connected. Either way the camera goes up as one **LiveKit** video track, the Live view shows that source's frames with each object's quick label, and the backend tracks every object and runs a Look on each (see [Pipeline](#pipeline)).

| | **Meta glasses** | **Phone camera** |
|---|---|---|
| Video source | Glasses camera → phone (DAT) → LiveKit | Phone camera (AVFoundation) → LiveKit |
| Shown on the phone | The glasses' frames, full screen | The phone camera's frames, full screen |
| How the user picks | **Tap** a quick label; **voice** once the agent is back | **Tap** a quick label (01a) |
| Results | On screen (01 / 01b), spoken through the glasses, plus a Live Activity | On screen, over the live view (01 / 01b) |

**Switching:**
- **Start screen:** "Phone camera" or "Meta glasses" (inert, with "No glasses found", until glasses connect; a link opens the Meta AI app to connect them).
- **Mid-stream:** Profile → "Stream from the glasses".
- **Glasses drop out** while they're the source → they stay the source: the DAT session waits and reconnects (a new session starts 2 s after one stops), and the status pill says "Connecting to glasses…" or, after a touchpad tap paused the stream, "Tap your glasses to resume". Switch to the phone camera in Profile.
- **One track:** both capturers feed the same LiveKit video track, so switching never republishes. The phone sends the `mode` RPC and the backend restarts tracking with new uids.

### Glasses mode: voice (when the agent returns)

The backend keeps a scene memory of the last ~15 s, and the voice agent's groundwork (`backend/storefront/voice`) resolves "that lamp" or "the other one" against it. The agent's behavior is removed for now. When it returns, it joins the room on `voice {on}`, calls `select {uid}` on the backend like a tap, and asks the phone to act through the `action` RPC. Showing the agent's pick on the phone comes back with it.

**Room recap (optional):** when the user leaves a room, the phone shows "You looked at 4 things here", built from the captures. It's opt-in.

### Tap the live video
1. The app opens on a start screen: pick what to look for (Things to buy, Furniture, Electronics, Kitchen, Clothing, Decor) and the camera, then **Start streaming** joins the room and starts it. The category goes to the backend with `POST /session/token`; it picks SAM's labels within it from the first frame, and until `status.prompts` arrives the Live view shows "Finding electronics in view…".
2. Each object's quick `label` (name and price estimate) pops up at the center of its box (01a). Objects without a label yet show nothing, and no box is drawn.
3. Tapping a label calls `select`: the label turns lime → searching card (01b) → results (01).

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
- **Pause:** the camera stops sending frames when paused, and the backend purges what it was tracking.

---

## Pipeline

**The backend tracks every object in the video and prices it on its own; the phone draws what it hears and follows the object the user taps.**

```
Phone ══ one video track (LiveKit, ~504×896 @ 15 fps, no simulcast) ══► LiveKit room ══► backend ("vision")
                                                                   SAM 3 tracking → objects (uid, box)
                                                       on screen 0.5 s → quick label (name, price range)
                                                       on screen 0.5 s → Look: describe → search stores → rank
Phone ◄══ topic "updates": objects · label · status (data packets) · look (text streams) ══┘
Phone ── RPC select {uid} ──► vision: answers that object's Look; its later steps follow as "look" messages
```

| Runs on | Steps |
|---|---|
| **Phone** (this repo) | Capture, preview, UI, cart, opening the checkout URL |
| **Backend** (FastAPI, `backend/storefront`) | Session tokens, tracking, quick labels, Looks |
| **LiveKit Cloud** | The room: video, messages, RPCs |
| **Anthropic API** | Claude Haiku: quick labels, descriptions, spoken summaries |
| **GPU server** | SAM 3 tracking |
| **External APIs** | Store search, Stripe |

### What the phone does

1. **Join:** `POST /session/token`, then join the room as `phone-…` and publish the camera (phone or glasses) as one video track. Send `mode` right after joining, and `pause` if paused.
2. **Draw:** each `objects` message moves the labels (in-view objects only); a `label` message adds one, at the center of its object's box. The app keeps nothing else: after a reconnect, the backend sends its current state again.
3. **Tap:** RPC `select {uid}`. Its answer is that object's Look as it stands: `started` / `described` → searching card (01b), `results` → product card (01), `failed` → couldn't tell (01c). Later `look` messages for that uid move the card along; Looks for other objects are ignored.
4. **Actions:** Save / Add to cart (client state). **Check out:** Face ID, then the checkout URL in `SFSafariViewController`.

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
    - `select_item {uid}`: the agent picked an item by voice. The app closes any sheet and selects it as a tap does, so its label and card show.

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

---

## App architecture

```
Spotted/
├─ App/        SpottedApp, AppState (live state, cart, saved, navigation, voice actions), AppModels,
│              LookActivityController
├─ Live/       LiveSession (the LiveKit room: video up, updates in, RPCs out)
│              VideoFeed (phone camera, mock room or glasses → the one video track)
├─ Mock/       MockData, MockSession (plays the server's messages without a backend)
├─ Network/    APIClient (POST /session/token), Models (mirrors backend/storefront/models.py), AppConfig
├─ Glasses/    GlassesManager (DAT registration, glasses camera stream)
├─ Voice/      VoiceAssistant (the agent in the shared room: transcripts, mic, "action" RPC), GlassesAudio
├─ Design/     Tokens, Glass
├─ Components/ Hotspot, DetectionBrackets, ProductCard, GlassTabBar, …
└─ Screens/    LiveView, AgentSheet, ProductDetail, SavedView, CartView, CheckoutSheet, glasses screens
```

**Data flow:**
- **Continuous:** `VideoFeed` → `LiveSession` (one video track up). Topic `updates` → `LiveSession` → `AppState.handle` → `objects` and `labels` → hotspots redraw.
- **On tap:** `AppState.select` → `LiveSession.select` (RPC) → the Look's latest step → `scan`. Later `look` messages for that uid update it.
- **Without a backend:** `MockSession` plays the same messages into `AppState.handle`.
- **Voice:** `VoiceAssistant` asks the backend for the agent (`voice {on}`) and publishes the mic; the agent's `action` RPC runs through `AppState`.

```swift
enum ScanState {
  case idle
  case looking                         // waiting for select's answer
  case detected(Look)                  // the Look is running: card in "searching"
  case matched(Look, SearchResponse)   // full card + verdict
  case failed(String)
}
```

---

## Screens

Each screen exists in Figma in both themes: **📱 Screens** (Lime) and **📱 Screens · Visa Night**.

The screens below (01–06) are the Live view and what follows it, for either camera. The glasses-mode screens (G2–G7) are listed after them.

| # | Frame | What it shows |
|---|---|---|
| 01a | Object Detected (Unselected) | The camera feed with each object's quick label ("Lounge chair · ~$900–1400") at its center, and a hint pill: "Tap the chair or say 'What's this?'". No card yet. |
| 01 | Live View | The chair is selected: its label turns lime, over a glass product card (Search / Save / Add to cart). The lamp keeps its glass label. |
| 02 | Listening → **Agent sheet** | A voice sheet with waveform, live transcript, the line the glasses will say, and suggestion chips. With the agent, this becomes the conversation surface with Claude (see below). |
| 03 | Product Detail | A hero image, a store comparison (lowest price tagged) and a floating glass checkout bar. |
| 04 | Saved | Saved items grouped by room ("Maya's apartment"). Saved = coral + filled bookmark + "Saved". |
| 05 | Cart | Line items, an **Agent limit** card (Visa), the summary, and Check out. |
| 06 | Checkout | A pay sheet showing merchants, card, limit check, the spoken confirmation, and **Pay with Face ID** / passcode. |
| 01b | Searching | While the Look runs: the lime label, crop thumbnail, the spoken quick phrase, skeleton prices and "Checking 3 stores…". Only Save is active. |
| 01c | Couldn't Tell | The Look failed: "Couldn't tell what that is", **Try again** / **Take a photo**. |
| 01d | Stream Reconnecting | 01a with a "Reconnecting…" pill and dimmed hotspots (the room dropped). |
| 03b | Dupes & Used | The Used tab with eBay results, and "Google Shopping · Unavailable right now" with Retry. |
| 06b | Stripe Handoff | A Safari sheet with a generic test-mode checkout form (`checkout.stripe.com`). |

**Flow:** 01a → tap → `select` → 01 (searching) → results → 01 (matched) → 03 / Save / Add → 05 → 06 → Stripe Checkout.

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
| G2 | Voice Command | While the user speaks: waveform, live transcript and the earcon state. |
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
| Unselected (01a) | Glass **quick label** ("Lounge chair · ~$900–1400") at the object's center; no box, no dot | none | tap the label |
| Selected (01) | The same label in **lime** | Product card | Save / Add / Search |

Labels pop in (scale and fade) when the backend names an object, and stay inside the screen near its edges. Transitions: 200 ms ease-out. Respect **Reduce Motion**: instant swap.

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
- **VoiceOver:** quick labels are buttons labeled with the item name and price; the product card reads name, price and actions in order.
- **Reduce Motion:** labels appear without the pop-in animation.

---

## Backend contract

Base URL: `https://dubhacks.tamkar.ai` by default, set in `Spotted.xcconfig`. Override it in the untracked `Config.xcconfig`, for example with a laptop's address or an ngrok URL.

> **Source of truth: `Network/Models.swift`**, which mirrors `backend/storefront/models.py`; change them together.
> - Decode with a plain `JSONDecoder()` (all keys are mapped in `CodingKeys`; don't set `keyDecodingStrategy`).
> - Optionals really can be `nil` at runtime. Never force-unwrap them.
> - Errors (any non-2xx, or an RPC answering `ok: false`) become `APIError { detail }`.

| Endpoint | Request | Response |
|---|---|---|
| `POST /session/token` | `TokenRequest` (`participant_identity`, `category`; both optional) | `TokenResponse` (`server_url`, `participant_token`): a new room with the backend in it, tracking `category` |
| `POST /checkout` | `CheckoutRequest` | `CheckoutResponse` (a stub for now) |

### The session's LiveKit room

The phone joins as `phone-…`, the backend as `vision`. One session at a time: a new token makes the backend leave the old room.

**Backend → phone**, topic `updates`:

| Message | Fields | When |
|---|---|---|
| `objects` (data) | `session`, `age_ms`, `objects: [{uid, box, prompt, score, in_view, last_seen_ts}]` | every tracking result |
| `label` (data) | `uid`, `box`, `name`, `price_low`, `price_high` | once per object, after 0.5 s on screen |
| `status` (data) | `tracking` (`stopped`, `starting`, `ok`, `offline`), `category`, `prompts` | when it changes |
| `look` (text stream) | `uid`, `look_id`, `stage` (`started`, `described`, `results`, `failed`), `label`, `box`, `selected`, `crop_url`, `item`, `search`, `message` | each step of an object's Look |

Boxes are `[x1, y1, x2, y2]`, normalized 0–1. `uid` is opaque. Someone joining mid-session gets the current state first.

**Phone → backend**, RPCs to `vision`, each answering `{"ok": true, …}` or `{"ok": false, "detail"}`:

| Method | Payload | Notes |
|---|---|---|
| `select` | `{uid}` | Answers `{"ok": true, "look": {…}}`, the object's Look as it stands |
| `mode` | `{mode: "glasses" or "phone"}` | after joining and on every camera switch |
| `pause` | `{paused}` | stops tracking and purges it |
| `voice` | `{on}` | brings the voice agent in, or sends it away |

The agent calls the phone's RPC `action` (`VoiceAction`), answered with a `VoiceActionReply`.

> **ngrok** only carries `POST /session/token`. The room runs on LiveKit Cloud, so video and messages don't go through ngrok and work across networks.

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
- Each returns `Cart { items, total, spokenSummary }`.
- Until the PR lands, keep the cart in `AppState` and switch to the server cart when the endpoints are live.

### UI rules
- **Hotspots from the stream:**
  - Each in-view `objects[]` entry with a `label` shows that label (01a) at the center of its `box`, keyed by `uid`. No box is drawn.
  - While the room is reconnecting or `status.tracking` is `offline`, show "Reconnecting…" in the status pill.
- **Selected:** tapping a label makes that object **selected** (the label turns lime) and calls `select`.
- **Couldn't tell:** a `failed` Look → show its `message` in **Couldn't tell** (01c).
- **Similarity:** show "Exact match" only when `product.isExactMatch` (`matchScore ≥ 85`). While `matchScore` is `nil` (today), show **"Similar"**.
- **Price verdict chip:** text is `bestMatchVerdict.label`. Color: Low = success, Fair = secondary, High = warning. Always text plus color.
- **Empty states:** `bestMatch == nil` → "No exact match for sale"; empty `dupes` or `used` → "No dupes found" / "No used listings".
- **Stale messages:** follow only the selected uid's `look` messages.
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
5. **Base URL:** `Spotted.xcconfig` defaults to `https://dubhacks.tamkar.ai`.
   - For another backend (a laptop or an ngrok URL, say), copy `Config.example.xcconfig` to the **untracked** `Config.xcconfig` and set `API_BASE_URL` there. Don't commit ngrok URLs: the repo is public, and every session costs LiveKit and Claude usage.
   - Debug builds also read `SPOTTED_API_BASE_URL` from the scheme; set it empty to run on mock data, as the UI tests do.
6. **Cloudflare Access:** the backend sits behind a Cloudflare tunnel protected by Access. Set the service token's `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` in `Config.xcconfig` (Cloudflare Zero Trust → Access → Service Auth). Requests to the backend's host, including crop images, carry them as `CF-Access-Client-Id` and `CF-Access-Client-Secret`; requests to other hosts never do. The token ships inside the app bundle, so use a token scoped to this app only.

---

## Build order

- [ ] 1. Meta sample app running with the glasses' camera visible on the phone
- [ ] 2. Own app with the live preview + `AppState`
- [ ] 3. Drop in the backend's `Models.swift`, then `APIClient` (`/detect`, `/search`, `/checkout`) against the real backend
- [ ] 4a. WebRTC: add `stasel/WebRTC`, publish a test track (phone camera) to the backend page via `POST /offer`, and confirm it plays on the laptop
- [ ] 4b. Swap in `GlassesVideoCapturer` (DAT frames → track); handle `detections` → live hotspots
- [ ] 4c. Select a hotspot → best full-res frame → `/detect` → card in searching state (01b)
- [ ] 5. `/search/{captureID}` → fill card: `bestMatch`, verdict chip, `verdictText`; handle nil/empty
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
- **Voice trigger for the demo:** push-to-talk (default), Siri / Action Button, or a foreground wake phrase?
- **Room recap:** in or out for the demo?

- **Theme:** Lime (default) or Visa Night for the demo? Both are ready in Figma.
- **Agent scope:** which tools ship for the demo? Must-haves: `save_item`, `add_to_cart`, `undo_last`, `show_results`, `start_checkout`. Stretch: `compare_items`, `list_saved(room:)`.
- **Agent memory:** does the session persist across app launches (e.g., "what did I save yesterday?"), or only within one session?
- **Assistant name:** "Spotted assistant" (powered by Claude) or a named persona? Keep the disclosure either way.
- **Phone "hover":** gaze vs press-and-hold for revealing the item tag.
- **Visa track story:** checkout runs on **Stripe test mode**. Decide how Visa shows up for judges: Visa test cards in Stripe, the "agent limit" concept, or a Visa sandbox API (Intelligent Commerce / Token Service) layered on. Ask the Visa mentors early.
- **`/agent` owner:** not on the backend yet. Decide who builds it, or fall back to on-device keyword commands (see the agent section).
- **T-Mobile Matchmaker:** get the brief from organizers. Ideas: nearest store with stock, "who else would love this?", 5G as the "instant" story.
- **Glasses hardware:** Ray-Ban Display (in-lens screen, gated preview) or audio-only Ray-Ban Meta?
