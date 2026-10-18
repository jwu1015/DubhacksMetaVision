import SwiftUI

/// G3 · Capture Card: the frame the backend used with the item highlighted, so the user can confirm
/// it picked the right thing, then the same searching / matched / couldn't-tell cards as 01.
struct CaptureCardView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                GlassIconButton(icon: "icon-back", label: "Back") { app.clearScan() }
                Spacer()
                Text(title)
                    .font(.spottedHeadline)
                    .foregroundStyle(.labelPrimary)
                    .lineLimit(1)
                Spacer()
                GlassIconButton(icon: "icon-more", label: "Matches and prices") { app.isShowingDetail = true }
                    .disabled(app.currentSearch == nil)
                    .opacity(app.currentSearch == nil ? 0.4 : 1)
            }
            .padding(.horizontal, 16)

            CapturedFrame(look: app.currentLook, detection: app.selectedDetection,
                          time: captureTime)
                .frame(height: 300)
                .padding(.horizontal, 16)
                .padding(.top, 10)

            card
                .padding(.horizontal, 12)
                .padding(.top, 16)

            if let next = app.nextBestDetection {
                SecondaryButton(title: "Not this one · try the \(next.label)", action: app.notThisOne)
                    .padding(.horizontal, 20)
                    .padding(.top, 12)
            }

            Spacer(minLength: 0)
        }
        .padding(.bottom, 86)
        // Tapping empty space outside the frame and card goes back to Glasses Home.
        .background {
            Color.ink
                .ignoresSafeArea()
                .onTapGesture { app.clearScan() }
        }
        .accessibilityAction(.escape) { app.clearScan() }
    }

    private var title: String {
        if case .failed = app.scan { return "Couldn't tell" }
        let label = app.currentLook?.label ?? app.selectedDetection?.label ?? "item"
        return "Price check · \(label)"
    }

    private var captureTime: Date {
        app.captures.first { $0.id == app.currentLook?.captureID }?.date ?? .now
    }

    @ViewBuilder private var card: some View {
        switch app.scan {
        case let .detected(look):
            SearchingCard(look: look, finishedSources: app.finishedSources,
                          isSaved: app.isSaved(look.name.sentenceCased), onSave: app.toggleSaveCurrent)
        case let .matched(look, search):
            ProductCard(look: look, search: search, isSaved: app.isSaved(look.name.sentenceCased),
                        onOpen: { app.isShowingDetail = true },
                        onSave: app.toggleSaveCurrent,
                        onAdd: app.addCurrentToCart)
        case let .failed(spokenLine):
            CouldntTellCard(spokenLine: spokenLine, onRetry: app.retry, onTakePhoto: app.retry)
        default:
            HintPill(icon: "icon-glasses", text: "Looking…")
        }
    }
}

/// The frame the backend used. The mock room is drawn with brackets on the item; a real Look shows
/// its crop, since the phone doesn't receive the glasses' full frame.
private struct CapturedFrame: View {
    let look: Look?
    let detection: Detection?
    let time: Date

    var body: some View {
        GeometryReader { geo in
            Group {
                if let detection, isMock {
                    mockRoom(around: detection, in: geo.size)
                } else if let source = look?.cutoutURL {
                    ProductImage(source: source)
                        .padding(24)
                        .frame(width: geo.size.width, height: geo.size.height)
                } else {
                    Color.surface
                }
            }
        }
        .background(.surface)
        .clipShape(RoundedRectangle(cornerRadius: 28, style: .continuous))
        .overlay(alignment: .bottomLeading) {
            HStack(spacing: 6) {
                Icon(name: "icon-glasses-small", size: 14)
                Text("From your glasses · \(time.formatted(date: .omitted, time: .shortened))")
                    .font(.spottedCaption)
                    .foregroundStyle(.labelPrimary)
            }
            .padding(.leading, 10)
            .padding(.trailing, 12)
            .padding(.vertical, 6)
            .glass(in: Capsule(), tint: 0.72)
            .padding(12)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Captured frame with the \(look?.label ?? detection?.label ?? "item") highlighted")
    }

    private var isMock: Bool { AppConfig.baseURL == nil || (look?.cutoutURL?.hasPrefix("asset:") ?? true) }

    /// The 393×852 mock room, shifted so the item sits inside the crop.
    private func mockRoom(around detection: Detection, in size: CGSize) -> some View {
        let canvas = CGSize(width: 393, height: 852)
        let rect = detection.rect(in: canvas)
        let dx = min(0, max(size.width - canvas.width, size.width / 2 - rect.midX))
        let dy = min(0, max(size.height - canvas.height, size.height / 2 - rect.midY))
        let box = rect.offsetBy(dx: dx, dy: dy)
        return ZStack(alignment: .topLeading) {
            MockCameraFeed()
                .frame(width: canvas.width, height: canvas.height)
                .offset(x: dx, y: dy)
            DetectionBrackets()
                .frame(width: box.width, height: box.height)
                .offset(x: box.minX, y: box.minY)
            DetectionTag(item: (look?.label ?? detection.label).sentenceCased)
                .fixedSize()
                .offset(x: min(box.minX, size.width - 130), y: box.minY - 34)
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
    }
}

#Preview("G3 · Capture Card") {
    let app = AppState()
    app.captureMode = .glasses
    app.scan = .matched(Look(MockData.lampDetect).finished(with: MockData.lampSearch), MockData.lampSearch)
    return RootView().environment(app)
}
