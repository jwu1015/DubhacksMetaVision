import LiveKit
import SwiftUI

/// 01 · Live: the glasses' feed with hotspots, the selected object's brackets and the product card.
/// Covers 01a, 01, 01b, 01c and 01d depending on `scan` and `connection`.
struct LiveView: View {
    @Environment(AppState.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.labels.count)
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
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.isFindingLabels)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.connection)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: app.blockedReason)
        .accessibilityAction(.escape, dismissSelection)
    }

    @ViewBuilder private var feed: some View {
        if let track = app.liveVideoTrack {
            SwiftUIVideoView(track, layoutMode: .fill, mirrorMode: .off)
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
        switch app.scan {
        case .looking, .detected, .matched, .failed: app.clearScan()
        case .idle: break
        }
    }

    /// In glasses mode, why no frames are coming in.
    private var glassesNote: String? {
        guard app.captureMode == .glasses, let camera = app.glasses?.camera else { return nil }
        switch camera {
        case .streaming: return nil
        case .paused: return "Tap your glasses to resume"
        case let .failed(message): return message
        case .idle, .connecting: return app.glasses?.errorMessage ?? "Connecting to glasses…"
        }
    }

    private var dimsFeed: Bool {
        app.isListening || app.isCameraPaused
    }

    // MARK: - Top

    @ViewBuilder private var topBar: some View {
        HStack(alignment: .top) {
            StatusPill(connection: app.connection, isPaused: app.isCameraPaused,
                       isGlassesMode: app.captureMode == .glasses, glassesNote: glassesNote)
            Spacer()
            GlassIconButton(icon: "icon-pause", iconSize: 18,
                            label: app.isCameraPaused ? "Resume camera" : "Pause camera") {
                app.isCameraPaused.toggle()
            }
        }
        .padding(.horizontal, 16)
    }

    // MARK: - Markers on the feed

    /// How many objects get their full label; the others get a dot.
    private static let maxLabels = 4

    @ViewBuilder
    private func markers(in size: CGSize) -> some View {
        if app.isCameraPaused {
            EmptyView()
        } else {
            // Objects that left the view in the last 15 s are still listed (for voice); only the ones in
            // view get a marker.
            let inView = app.objects.filter(\.inView)
            let labeled = labeledUIDs(among: inView)
            ForEach(inView) { object in
                marker(for: object, showsLabel: labeled.contains(object.uid), in: size)
            }
        }
    }

    /// The named objects that keep their full label: the top 4 by 0.2 × likelihood + 0.8 × box size,
    /// plus the selected one so its label never turns into a dot.
    private func labeledUIDs(among objects: [LiveObject]) -> Set<String> {
        func rank(_ object: LiveObject) -> Double {
            let box = object.bbox
            return 0.2 * object.score + 0.8 * max(box.w, 0) * max(box.h, 0)
        }
        var uids = Set(objects.filter { app.labels[$0.uid] != nil }
            .sorted { rank($0) > rank($1) }
            .prefix(Self.maxLabels)
            .map(\.uid))
        if isSelecting, let selected = app.selectedUID, app.labels[selected] != nil {
            uids.insert(selected)
        }
        return uids
    }

    /// At the center of the object's box: its quick label once the backend has named it and it ranks
    /// in the top 4, otherwise a small dot.
    @ViewBuilder
    private func marker(for object: LiveObject, showsLabel: Bool, in size: CGSize) -> some View {
        let rect = feedRect(object.bbox, in: size)
        let isSelected = object.uid == app.selectedUID && isSelecting
        if showsLabel, let label = app.labels[object.uid] {
            ClampedPosition(center: CGPoint(x: rect.midX, y: rect.midY)) {
                Hotspot(item: label.name.sentenceCased, priceEstimate: label.priceEstimate, isSelected: isSelected)
                    .opacity(hotspotOpacity)
                    // A tap on the selected object itself keeps it selected.
                    .onTapGesture { if !isSelected { app.select(object) } }
                    .accessibilityAction { app.select(object) }
            }
            .transition(.scale(scale: 0.6).combined(with: .opacity))
        } else {
            ClampedPosition(center: CGPoint(x: rect.midX, y: rect.midY)) {
                HotspotDot(name: app.labels[object.uid]?.name.sentenceCased ?? object.prompt)
                    .opacity(hotspotOpacity)
                    .onTapGesture { app.select(object) }
                    .accessibilityAction { app.select(object) }
            }
            .transition(.scale(scale: 0.6).combined(with: .opacity))
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
        if let reason = app.blockedReason {
            BlockedCard(reason: reason) { app.isCameraPaused = false }
                .padding(.horizontal, 12)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        } else {
            scanContent
        }
    }

    @ViewBuilder private var scanContent: some View {
        switch app.scan {
        case .idle where app.isFindingLabels:
            LabelsLoader(category: app.category)
                .padding(.bottom, 12)
                .transition(.opacity)
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

/// The session failed its security check: recording isn't OK here, so the camera is paused.
private struct BlockedCard: View {
    let reason: String
    let onResume: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Icon(name: "icon-shield", size: 22)
                    .frame(width: 44, height: 44)
                    .background(.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text("Can't record here")
                        .font(.storefrontHeadline)
                        .foregroundStyle(.labelPrimary)
                    Text(reason)
                        .font(.storefrontFootnote)
                        .foregroundStyle(.labelSecondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Text("The camera is paused. Move somewhere else, then try again.")
                .font(.storefrontFootnote)
                .foregroundStyle(.labelSecondary)
            PrimaryButton(title: "Try again", icon: "icon-retry-on-accent", action: onResume)
        }
        .padding(16)
        .glass(in: RoundedRectangle(cornerRadius: 32, style: .continuous))
        .accessibilityElement(children: .contain)
    }
}

/// While the backend picks what to track from the first frame: no label can show up before that.
private struct LabelsLoader: View {
    let category: String

    var body: some View {
        HStack(spacing: 12) {
            ProgressView()
                .tint(.labelPrimary)
            VStack(alignment: .leading, spacing: 2) {
                Text("Finding \(category) in view…")
                    .font(.storefrontSubhead)
                    .foregroundStyle(.labelPrimary)
                Text("Picking what to look for from the first frame")
                    .font(.storefrontFootnote)
                    .foregroundStyle(.labelSecondary)
            }
        }
        .padding(.leading, 16)
        .padding(.trailing, 20)
        .padding(.vertical, 12)
        .glass(in: Capsule(), tint: 0.62)
        .accessibilityElement(children: .combine)
    }
}

/// Centers its one subview on `center`, nudged to stay inside the bounds: a label on an object at the
/// edge of the frame stays readable.
private struct ClampedPosition: Layout {
    let center: CGPoint
    var margin: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        proposal.replacingUnspecifiedDimensions()
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard let view = subviews.first else { return }
        let size = view.sizeThatFits(.unspecified)
        let x = min(max(center.x, size.width / 2 + margin), bounds.width - size.width / 2 - margin)
        let y = min(max(center.y, size.height / 2 + margin), bounds.height - size.height / 2 - margin)
        view.place(at: CGPoint(x: bounds.minX + x, y: bounds.minY + y), anchor: .center, proposal: .unspecified)
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
