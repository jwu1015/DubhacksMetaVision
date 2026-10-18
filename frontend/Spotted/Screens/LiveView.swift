import LiveKit
import SwiftUI

/// 01 · Live: the glasses' feed with hotspots, the selected object's brackets and the product card.
/// Covers 01a, 01, 01b, 01c and 01d depending on `scan` and `connection`.
struct LiveView: View {
    @Environment(AppState.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var focusedUID: String?

    var body: some View {
        ZStack {
            GeometryReader { geo in
                ZStack(alignment: .topLeading) {
                    feed
                        // Tapping the feed outside the card dismisses the selection.
                        .contentShape(Rectangle())
                        .onTapGesture(perform: dismissSelection)
                    if dimsFeed {
                        Color.black.opacity(0.35)
                    }
                    markers(in: geo.size)
                }
            }
            .ignoresSafeArea()

            VStack(spacing: 0) {
                if !app.isListening { topBar }
                Spacer(minLength: 0)
                if !app.isListening {
                    bottomContent
                }
            }
            // Tab bar (62) + 12pt gap.
            .padding(.bottom, 74)
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.scan)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.connection)
        .accessibilityAction(.escape, dismissSelection)
    }

    @ViewBuilder private var feed: some View {
        if let track = app.liveVideoTrack {
            SwiftUIVideoView(track, layoutMode: .fill)
        } else {
            MockCameraFeed()
        }
    }

    /// Normalized frame box → screen. The camera preview is aspect-filled, so map through the crop;
    /// the mock feed is stretched to the screen, so it maps directly.
    private func feedRect(_ box: BBox, in size: CGSize) -> CGRect {
        guard let aspect = app.previewAspect else { return box.rect(in: size) }
        let height = max(size.height, size.width / aspect)
        let width = height * aspect
        let origin = CGPoint(x: (size.width - width) / 2, y: (size.height - height) / 2)
        return CGRect(x: origin.x + box.x * width, y: origin.y + box.y * height,
                      width: box.w * width, height: box.h * height)
    }

    /// Back to the hotspots: clears the product, searching or couldn't-tell card and any open tag.
    private func dismissSelection() {
        focusedUID = nil
        switch app.scan {
        case .looking, .detected, .matched, .failed: app.clearScan()
        case .idle: break
        }
    }

    private var dimsFeed: Bool {
        app.isListening || app.isCameraPaused
    }

    // MARK: - Top

    @ViewBuilder private var topBar: some View {
        HStack(alignment: .top) {
            StatusPill(connection: app.connection, isPaused: app.isCameraPaused)
            Spacer()
            GlassIconButton(icon: "icon-pause", iconSize: 18,
                            label: app.isCameraPaused ? "Resume camera" : "Pause camera") {
                app.isCameraPaused.toggle()
            }
        }
        .padding(.horizontal, 16)
    }

    // MARK: - Markers on the feed

    @ViewBuilder
    private func markers(in size: CGSize) -> some View {
        if app.isCameraPaused {
            EmptyView()
        } else {
            // Objects that left the view in the last 15 s are still listed (for voice); only the ones in
            // view get a hotspot.
            ForEach(app.objects.filter(\.inView)) { object in
                marker(for: object, in: size)
            }
        }
    }

    @ViewBuilder
    private func marker(for object: LiveObject, in size: CGSize) -> some View {
        let rect = feedRect(object.bbox, in: size)
        if object.uid == app.selectedUID, isSelecting {
            DetectionBrackets()
                .frame(width: rect.width, height: rect.height)
                // A tap on the selected object itself keeps it selected.
                .contentShape(Rectangle())
                .onTapGesture {}
                .offset(x: rect.minX, y: rect.minY)
            if let look = app.currentLook {
                DetectionTag(item: look.name.sentenceCased)
                    .fixedSize()
                    .offset(x: rect.minX, y: rect.minY - 32)
            }
        } else {
            let label = app.labels[object.uid]
            Hotspot(item: (label?.name ?? object.prompt).sentenceCased, priceEstimate: label?.priceEstimate,
                    isFocused: focusedUID == object.uid)
                .opacity(hotspotOpacity)
                .offset(x: rect.midX - 32, y: rect.midY - 32)
                .onTapGesture {
                    focusedUID = nil
                    app.select(object)
                }
                // Press-and-hold stands in for Figma's Hover state on touch.
                .onLongPressGesture(minimumDuration: 0.4) {
                    focusedUID = object.uid
                }
                .accessibilityAction { app.select(object) }
        }
    }

    private var isSelecting: Bool {
        switch app.scan {
        case .looking, .detected, .matched: true
        default: false
        }
    }

    private var hotspotOpacity: Double {
        if case .failed = app.scan { return 0.35 }
        return app.connection == .reconnecting ? 0.4 : 1
    }

    // MARK: - Bottom

    @ViewBuilder private var bottomContent: some View {
        switch app.scan {
        case .idle:
            HintPill(text: "Tap the \(app.objects.first?.prompt ?? "item") or say “What’s this?”")
                .padding(.bottom, 12)
                .transition(.opacity)
        case .looking:
            HintPill(icon: "icon-glasses", text: "Looking…")
                .padding(.bottom, 12)
                .transition(.opacity)
        case let .detected(look):
            SearchingCard(look: look, isSaved: app.isSaved(look.name.sentenceCased), onSave: app.toggleSaveCurrent)
                .padding(.horizontal, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        case let .matched(look, search):
            ProductCard(look: look, search: search,
                        isSaved: app.isSaved(look.name.sentenceCased),
                        onOpen: { app.isShowingDetail = true },
                        onSave: app.toggleSaveCurrent,
                        onAdd: app.addCurrentToCart)
                .padding(.horizontal, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        case let .failed(spokenLine):
            // TODO: "Take a photo" should grab a 12 MP capturePhoto still once GlassesManager exists.
            CouldntTellCard(spokenLine: spokenLine, onRetry: app.retry, onTakePhoto: app.retry)
                .padding(.horizontal, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }
}

#Preview("01 · Live View") {
    let app = AppState()
    app.selectedUID = MockData.chair.uid
    app.scan = .matched(MockData.chairResults, MockData.search)
    return RootView().environment(app)
}

#Preview("01a · Unselected") {
    RootView().environment(AppState())
}

#Preview("01b · Searching") {
    let app = AppState()
    app.selectedUID = MockData.chair.uid
    app.scan = .detected(MockData.chairDescribed)
    return RootView().environment(app)
}
