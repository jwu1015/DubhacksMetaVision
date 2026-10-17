import SwiftUI

/// 01 · Live: the glasses' feed with hotspots, the selected object's brackets and the product card.
/// Covers 01a, 01, 01b, 01c, 01d, 07 and 08 depending on `scan` and `connection`.
struct LiveView: View {
    @Environment(AppState.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var focusedTrackID: String?

    var body: some View {
        ZStack {
            GeometryReader { geo in
                ZStack(alignment: .topLeading) {
                    MockCameraFeed()
                        .opacity(app.connection == .disconnected ? 0.35 : 1)
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
                if app.connection == .disconnected {
                    DisconnectedView()
                    Spacer(minLength: 0)
                } else if !app.isListening {
                    bottomContent
                }
            }
            // Tab bar (62) + 12pt gap.
            .padding(.bottom, 74)
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.scan)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.connection)
    }

    private var dimsFeed: Bool {
        app.connection == .disconnected || app.isListening || app.isCameraPaused
    }

    // MARK: - Top

    @ViewBuilder private var topBar: some View {
        if app.connection == .disconnected {
            DisconnectedBanner()
                .padding(.horizontal, 16)
                .padding(.top, 54)
        } else {
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
    }

    // MARK: - Markers on the feed

    @ViewBuilder
    private func markers(in size: CGSize) -> some View {
        if app.connection == .disconnected || app.isCameraPaused {
            EmptyView()
        } else if case let .ambiguous(candidates) = app.scan {
            ForEach(Array(candidates.enumerated()), id: \.element.id) { index, candidate in
                let rect = candidate.detection.rect(in: size)
                DetectionBrackets(style: .white)
                    .frame(width: rect.width, height: rect.height)
                    .contentShape(Rectangle())
                    .offset(x: rect.minX, y: rect.minY)
                    .onTapGesture { app.select(candidate.detection) }
                OptionBadge(letter: OptionBadge.letter(for: index), size: 32)
                    .offset(x: rect.minX - 12, y: rect.minY - 16)
            }
        } else {
            ForEach(app.detections.filter { $0.score >= 0.6 }) { detection in
                marker(for: detection, in: size)
            }
        }
    }

    @ViewBuilder
    private func marker(for detection: Detection, in size: CGSize) -> some View {
        let rect = detection.rect(in: size)
        if detection.trackID == app.selectedTrackID, isSelecting {
            DetectionBrackets()
                .frame(width: rect.width, height: rect.height)
                .offset(x: rect.minX, y: rect.minY)
            if let detect = app.currentDetect {
                DetectionTag(item: detect.category.sentenceCased)
                    .fixedSize()
                    .offset(x: rect.minX, y: rect.minY - 32)
            }
        } else {
            Hotspot(item: detection.label.sentenceCased, isFocused: focusedTrackID == detection.trackID)
                .opacity(hotspotOpacity)
                .offset(x: rect.midX - 32, y: rect.midY - 32)
                .onTapGesture {
                    focusedTrackID = nil
                    app.select(detection)
                }
                // Press-and-hold stands in for Figma's Hover state on touch.
                .onLongPressGesture(minimumDuration: 0.4) {
                    focusedTrackID = detection.trackID
                }
                .accessibilityAction { app.select(detection) }
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
            HintPill(text: "Tap the \(app.detections.first?.label ?? "item") or say “What’s this?”")
                .padding(.bottom, 12)
                .transition(.opacity)
        case .looking:
            HintPill(icon: "icon-glasses", text: "Looking…")
                .padding(.bottom, 12)
                .transition(.opacity)
        case let .detected(detect):
            SearchingCard(detect: detect, finishedSources: app.finishedSources,
                          isSaved: app.isSaved(detect.category.sentenceCased), onSave: app.toggleSaveCurrent)
                .padding(.horizontal, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        case let .matched(detect, search):
            ProductCard(detect: detect, search: search,
                        isSaved: app.isSaved(search.bestMatch?.title ?? detect.category.sentenceCased),
                        onOpen: { app.isShowingDetail = true },
                        onSave: app.toggleSaveCurrent,
                        onAdd: app.addCurrentToCart)
                .padding(.horizontal, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        case let .ambiguous(candidates):
            DisambiguationCard(candidates: candidates,
                               onPick: { app.select($0.detection) },
                               onNeither: app.clearScan)
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
    app.show(.matched)
    return RootView().environment(app)
}

#Preview("01a · Unselected") {
    RootView().environment(AppState())
}

#Preview("01b · Searching") {
    let app = AppState()
    app.show(.searching)
    return RootView().environment(app)
}
