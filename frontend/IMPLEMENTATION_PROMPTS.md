# Implementation prompts: two-mode pivot

A series of prompts to paste into a coding agent (e.g., Claude Code) working on the `front_end` branch of `DubhacksMetaVision`.

**How to use these**
- Run them **in order**, one per session or turn.
- **Build in Xcode after each prompt** and commit before moving on.
- Every prompt assumes the agent can read `frontend/FRONTEND.md` (the spec) and the existing SwiftUI code in `frontend/Spotted/`.

**Ground rules (included in prompt 0):**
- Don't edit `Network/Models.swift`. It's owned by the backend; put helpers in `App/AppModels.swift`.
- Never commit the backend base URL. It lives in an untracked `Config.xcconfig` / `Config.swift`.
- Keep mock data working, so every screen still previews without a backend or glasses.
- Use the existing design tokens (`Design/Tokens.swift`), glass helpers (`Design/Glass.swift`) and components. Use SwiftUI system text styles.

---

## 0 · Context primer (paste first in every new session)

> You're working on **Spotted**, a SwiftUI iOS app in `frontend/Spotted/` (Xcode project `frontend/Spotted.xcodeproj`, folder-synced groups, so new files are picked up automatically). Read `frontend/FRONTEND.md` fully, especially "Modes: glasses vs phone", "Claude shopping agent", "Backend contract" and "Build order → Two-mode pivot".
>
> **Summary:** the app has two modes and one pipeline.
> - **Glasses mode:** the Meta glasses' video streams over WebRTC to the backend but is **not shown**. The user picks items **by voice**, resolved against recent detections (scene memory).
> - **Phone mode:** the phone camera is shown live, and the user **taps hotspots**.
> - **Both modes** send video over WebRTC and get `detections` back on a data channel.
>
> **Rules:**
> - Don't modify `Network/Models.swift` (it belongs to the backend team).
> - Put app helpers in `App/AppModels.swift`.
> - Never commit the base URL. Keep `MockData` working so every screen has a `#Preview`.
> - Reuse `Design/Tokens.swift`, `Design/Glass.swift` and the existing components.
> - Follow the voice rules in FRONTEND.md: spoken lines ≤ 8 words, and Face ID for any payment.
>
> Before editing, list the files you plan to change. After editing, list what changed and anything you couldn't verify without building.

---

## 1 · Capture mode + switching

> Add a capture mode to the app.
> - In `App/AppModels.swift`, add `enum CaptureMode { case glasses, phone }`.
> - In `AppState`, add `var captureMode: CaptureMode` plus `var glassesConnected: Bool`. The mode is derived: glasses if connected, else phone.
> - When `glassesConnected` changes, show a toast:
>   - "Glasses connected. Say 'Spotted' to capture."
>   - "Glasses disconnected. Using your phone camera."
>
>   Build a small reusable `Toast` component in `Components/` (glass capsule, auto-dismiss after 3 s, VoiceOver announcement via `AccessibilityNotification.Announcement`).
> - `RootView`: the Live tab shows `LiveView` (phone mode, existing) or a new `GlassesHomeView` (glasses mode; a placeholder for now, built in prompt 8).
> - In `ProfileView`, add a debug toggle "Simulate glasses connected" so both modes can be demoed without hardware.
>
> **Done when:** toggling the debug switch swaps the Live tab between the two views and shows the toast; previews exist for both.

## 2 · API client + config

> Implement `Network/APIClient.swift` against the types in `Network/Models.swift` (read it first; don't edit it).
> - Base URL from an untracked `Config.xcconfig` key `API_BASE_URL`, exposed through Info.plist.
>   - Add `frontend/Config.example.xcconfig` with a placeholder.
>   - Confirm `.gitignore` already ignores `frontend/**/Config.xcconfig`.
> - `async throws` methods:
>   - `health() -> HealthResponse`
>   - `detect(image: Data, hint: DetectHint?) -> DetectResponse`
>   - `detect(trackID: String) -> DetectResponse`
>   - `search(captureID:) -> SearchResponse` (30 s timeout)
>   - `checkout(_ items: [Product]) -> CheckoutResponse`
>   - `addToCart`, `getCart`, `removeFromCart(index:)`
>
>   `DetectHint` (define in `AppModels.swift`) is `.box(BBox)`, `.point(x:y:)` or `.text(String)`, sent as the multipart fields `hint_box`, `hint_point` and `hint_text`. `detect(trackID:)` sends multipart `track_id` with no image.
> - Decode non-2xx responses as `APIError`. Use a plain `JSONDecoder()`.
> - Add `protocol SpottedAPI` with `APIClient` and `MockAPIClient` (returns `MockData` with realistic delays: detect 1.5 s, search 4 s). `AppState` takes a `SpottedAPI` and uses the mock when no base URL is configured.
> - Replace the mocked pipeline in `AppState.select(_:)` with: `api.detect(trackID:)` → on `404 "track expired"`, fall back to uploading `FrameBuffer`'s frame with `.box` → `api.search`. Keep the stale-`captureID` guard and the `finishedSources` animation (finish all sources when search returns).
>
> **Done when:** the app runs the full flow against `MockAPIClient`, and against the real backend when `Config.xcconfig` exists.

## 3 · Phone camera source (phone mode)

> Build the phone camera pipeline for phone mode.
> - `Camera/PhoneCamera.swift`: an AVFoundation `AVCaptureSession` using the back wide camera at 1280×720 / 30 fps, with a `CMSampleBuffer` output delegate on a background queue.
>   - Request camera permission with a friendly pre-prompt screen.
>   - Handle denied permission with a settings deep link.
> - `Camera/CameraPreview.swift`: a `UIViewRepresentable` wrapping `AVCaptureVideoPreviewLayer`, aspect fill.
> - `Camera/FrameBuffer.swift`: keep the sharpest of the last ~1 s of frames as a `CGImage`, using a cheap sharpness score (e.g., variance of a downsampled grayscale). It must expose `func bestJPEG(maxLongSide: 2048, quality: 0.8) -> Data?`.
> - In `LiveView`, replace `MockCameraFeed` with `CameraPreview` when running on a device with permission. Keep `MockCameraFeed` for previews and the simulator.
> - Hotspot coordinates: map normalized boxes to the **visible** preview rect, accounting for aspect-fill cropping. Add a helper for this and unit-test it.
>
> **Done when:** on a device, the live camera shows under the existing hotspots and overlays, and the simulator still shows the mock feed.

## 4 · WebRTC publisher (both modes)

> Add WebRTC with the `stasel/WebRTC` Swift package.
> - `Network/WebRTCPublisher.swift`:
>   - **Peer connection:** create an `RTCPeerConnection` (STUN `stun:stun.l.google.com:19302`) with one **send-only** video track and a data channel named `"detections"`.
>   - **Signaling:** `POST {base}/offer` with `SessionDescription(sdp:type:"offer")`, then apply the answer.
>   - **Detections in:** decode `DetectionsMessage` (in Models.swift) and publish `objects` to `AppState.detections`. Ignore messages with a `ts` older than the last applied one.
>   - **Control messages out:** `send(mode:)`, `send(paused:)`, `send(select trackID:)`, encoding `{"type":"mode","mode":...}`, `{"type":"pause","paused":...}` and `SelectMessage`.
>   - **Source swap:** `setSource(_ capturer: RTCVideoCapturer)`, swapping the source on the same sender with `replaceTrack`, with no renegotiation.
>   - **Frame rate:** `setFrameRate(_ fps: Int)` using sender encoding parameters (`maxFramerate`, `maxBitrateBps`).
> - `Camera/PhoneVideoCapturer.swift`: feed `PhoneCamera` sample buffers into an `RTCVideoSource` (`RTCCVPixelBuffer` → `RTCVideoFrame`) and also into `FrameBuffer`.
> - **Connection state** maps to `AppState.connection` (live / reconnecting / disconnected), driving the existing 01d "Reconnecting…" state.
> - **When to stop:** stop sending video when paused, or when the app is in the background with no capture in flight.
> - **Fallback:** if `/offer` fails, keep the app usable. Hotspots fall back to mock detections in debug builds, and tapping still works via image upload + `hint_point`.
>
> **Done when:** on a device in phone mode, the backend's WebRTC page shows the phone video, and hotspots come from real `detections`.

## 5 · Glasses source (glasses mode, no preview)

> Integrate the Meta Wearables DAT SDK (`https://github.com/facebook/meta-wearables-dat-ios`; read its sample app first) for glasses mode.
> - `Glasses/GlassesManager.swift`: registration (Meta AI app callback via URL scheme) and a stream session at 504×896.
>   - Expose `isConnected` so it drives `AppState.glassesConnected`.
>   - Optionally expose `capturePhoto() async -> Data?` (12 MP JPEG) for a sharper fallback.
> - `Glasses/GlassesVideoCapturer.swift`: DAT `VideoFrame.sampleBuffer` → `RTCVideoFrame` into the WebRTC source, and also into `FrameBuffer`. **Render no preview** in glasses mode.
> - On mode change, call `WebRTCPublisher.setSource(...)` and `send(mode:)`.
> - **Frame rate:** default 5 fps when idle, 15 fps while a voice command is active (prompt 6) or a capture is in flight.
> - **Info.plist:** add the `MWDAT` dictionary, the URL scheme, Bluetooth background mode, and usage descriptions, per FRONTEND.md → Setup. Values that are secret or per-developer go in the untracked xcconfig.
>
> **Done when:** with glasses paired, the mode switches automatically, the backend receives the glasses' video, and nothing is rendered on the phone except the glasses-mode UI.

## 6 · Voice: push-to-talk, local commands, speaker

> Build voice input and output.
> - `Voice/SpeechInput.swift`: on-device `SFSpeechRecognizer` with `AVAudioSession` set to `.playAndRecord` with `.allowBluetooth` / `.allowBluetoothA2DP`, so the glasses' mic and speakers are used when connected. The API is push-to-talk `start()` / `stop()`, publishing a partial transcript and a final transcript.
> - `Voice/LocalCommands.swift`: handle "stop", "never mind", "cancel", "undo" and "pause camera" / "resume camera" **on device**, with no network.
> - `Voice/Speaker.swift`:
>   - Speech: `AVSpeechSynthesizer` with a queue, interruptible (barge-in stops speech immediately).
>   - Earcons: short system sounds or bundled `.caf` files for start-listening, captured, success and error.
> - Wire up the existing mic button (tab bar) and the Agent sheet (02): press-and-hold to talk, release to send.
>
> **Done when:** holding the mic shows a live transcript, "stop" cuts off speech instantly, and spoken lines play through the glasses when connected.

## 7 · Agent + target resolution (glasses-mode core)

> Implement voice → item for glasses mode.
> - `Agent/AgentClient.swift`: the cut-down `POST /agent` from `BACKEND_REQUESTS.md` (R5). The request is `{session_id, message, mode, context}`; the response is `{speech, action, target, resolved?, ask?}`. Add these request and response types to `AppModels.swift`, not Models.swift.
> - `Agent/TargetResolver.swift`: an **on-device fallback** used when `/agent` is unavailable.
>   - **Parse:** keyword-parse the action (price/how much → price_check, save → save, add/cart → add_to_cart, buy → buy) and the target noun.
>   - **Rank** recent `detections`: label match, then in view and centered (distance of box center to frame center) > most recent > longest dwell. Track dwell over the last 15 s from `detections` history.
>   - "That" with no noun means the most centered object in view.
>   - Return `.resolved(trackID)` or `.ask([candidateA, candidateB])` when the top two scores are within 15%.
> - **Flow** in `AppState.handleVoiceCommand(_ text:)`:
>   1. Speak `speech` (or "Checking the lamp.").
>   2. `.resolved` → `detect(trackID:)` (fallback: upload the best frame + `.text(target)` hint) → speak `quickPhrase` → `search` → speak `spokenSummary` → apply the action (save / add to cart via the existing methods). `buy` opens checkout behind Face ID.
>   3. `.ask` → G4 (prompt 11).
>   4. Nothing found → "Couldn't find a lamp. Look at it and try again."
> - Create a `Capture` record (in `AppModels.swift`: id, captureID, mode, frame image URL, bbox, detect, search, action, room, timestamp) and append it to `AppState.captures`.
>
> **Done when:** in simulated glasses mode with mock detections, saying "how much is that lamp" resolves the lamp, speaks the answer, and adds a capture.

## 8 · Glasses home (G1) + voice command (G2)

> Build `Screens/GlassesHomeView.swift` (G1) and the voice command state (G2) per FRONTEND.md → "Glasses-mode screens".
> - **G1:**
>   - **No video.** Calm ink background with a subtle animated glasses glyph (static under Reduce Motion).
>   - **Status pill:** "Glasses · Connected" plus "Glasses camera on · not recording" with a **Pause** button (sends `pause`, stops video, and changes the copy to "Camera paused").
>   - **Push-to-talk:** a large mic button (≥ 62 pt), with the hint "Say 'Spotted, how much is that lamp?'".
>   - **Latest captures:** the last 3, as compact rows linking to G3.
> - **G2:** while talking, show a waveform, the live transcript, and **parsed chips** ("Price check · lamp") as soon as the agent or resolver returns, plus "Checking…".
> - Use the existing glass components and tokens; it must work in the Lime and Visa Night themes. Add previews for idle, listening, resolving and paused.
>
> **Done when:** the glasses-mode Live tab is fully usable with mock data and matches the existing design language.

## 9 · Capture card (G3) + "Not this one"

> Build `Components/CaptureCard.swift` (G3), presented after a glasses-mode capture and from the history feed.
> - **Top:** the frame the backend used (`DetectResponse.imageURL`) with the item highlighted, using the existing `DetectionBrackets` on `segmentation.bbox` (normalized x/y/w/h) and a dimmed scrim outside the box.
> - **Below:** reuse `SearchingCard` content while searching and `ProductCard` content when matched (refactor shared pieces rather than duplicating).
> - **"Not this one":** re-resolves to the next-best candidate (TargetResolver or `/agent`) and re-runs detect and search, replacing the capture.
> - **Accessibility:** VoiceOver reads "Captured: lamp, $189, fair price".
>
> **Done when:** a glasses capture shows its frame with the highlight, fills in results, and can be corrected.

## 10 · Spotted Today feed (G5)

> Build `Screens/SpottedTodayView.swift` (G5): a feed of `AppState.captures` grouped by room (reuse `Room`), newest first.
> - **Each row:** frame thumbnail with the highlight, item title, price, verdict chip, and Save / Add to cart.
> - **Privacy line** at the top: "Only things you asked about are kept here."
> - Swipe to delete.
> - **Placement:** reachable from G1's "Latest captures → See all". In phone mode, put it in the Saved tab as a "History" segment (the segmented control already exists there).
> - **Empty state:** "Nothing spotted yet. Say 'Spotted, what's that?'"
>
> **Done when:** captures from both modes appear, grouped by room, with working actions.

## 11 · Voice which-one (G4)

> Build the glasses-mode disambiguation (G4).
> - When the resolver or agent returns `.ask`:
>   - **Speak** "Floor lamp or desk lamp?"
>   - **Show** two cropped thumbnails, cropped from `FrameBuffer` (or the backend frame) by each candidate's box, labeled with their names. Tapping either picks it.
>   - **Listen** for the answer: match the spoken reply against the candidate labels, or "first"/"second", "left"/"right".
> - Reuse `DisambiguationCard` (07) styling where possible.
> - **Timeout:** after 8 s with no answer, say "Okay, cancelled." and dismiss.
>
> **Done when:** two similar mock detections trigger G4, and a spoken or tapped answer continues the capture flow.

## 12 · Live Activity (G6)

> Add a Live Activity with ActivityKit for glasses-mode captures.
> - Create a **Widget Extension** target (`SpottedWidgets`) with a Live Activity.
>   - **This needs Xcode UI.** Give me exact step-by-step instructions for creating the target, then write the code files.
>   - Shared `ActivityAttributes`: item label and capture ID; content state is phase (`finding` / `matched` / `failed`), price, verdict and store.
> - **Views:**
>   - Lock screen: thumbnail, "Finding prices · lamp…" → "Lamp · $189 · Fair price".
>   - Dynamic Island: compact (label + price), minimal (accent dot) and expanded (Save / Add to cart buttons).
>   - The buttons use `LiveActivityIntent` App Intents that call into the app.
> - **Lifecycle:**
>   - Start on capture, update when search returns, end after 2 minutes.
>   - Add `NSSupportsLiveActivities` to Info.plist.
>
> **Done when:** a capture shows on the lock screen and Dynamic Island, and the Save button works.

## 13 · Privacy, battery, polish

> Do a hardening pass on the two-mode flow.
> - **Privacy:**
>   - The status pill is always visible in glasses mode.
>   - "Pause camera" by voice and button.
>   - Stop video when the app is in the background (unless a capture is in flight).
>   - Clear in-memory `FrameBuffer` and detections on pause or disconnect.
> - **Battery and thermal:**
>   - 5 fps when idle, 15 fps during commands.
>   - Watch `ProcessInfo.thermalState`; at `.serious`, drop to 3 fps and show a small "Saving power" note.
> - **Mode switch mid-capture:** finish the capture with the frame already sent; don't crash or double-speak.
> - **Errors** (per FRONTEND.md voice rules):
>   - "Couldn't reach Spotted. Try again."
>   - Track expired → silent fallback to frame upload.
> - **Accessibility:** a VoiceOver pass on G1–G5, Dynamic Type at AX sizes, Reduce Motion.
>
> **Done when:** all of the above works, and you've listed anything you couldn't verify without a device.

## 14 · Stretch: Siri / Action Button + room recap

> **Stretch features:**
> 1. An **App Intent** `SpotThisIntent` ("Spotted this" / "Spot this with Spotted") with an App Shortcut phrase. Running it starts push-to-talk listening in glasses mode, or captures the most centered object in phone mode. It's assignable to the iPhone Action Button.
> 2. **Room recap (G7):** when the user switches rooms (manual "New room" button for the demo), show "You looked at N things at {room}", built from tracks with `seconds_viewed ≥ 2` in the last session. Include Save all / pick, and make it opt-in in Profile.
>
> **Done when:** "Hey Siri, Spotted this" starts a capture, and the recap appears after switching rooms.

---

## After each prompt
- Build and run in Xcode (a device for 3–5 and 12).
- Commit with a clear message.
- Paste any build errors back to the agent with: "Fix these build errors without changing behavior: …"
