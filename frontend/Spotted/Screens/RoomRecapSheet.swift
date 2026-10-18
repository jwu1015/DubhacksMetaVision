import SwiftUI

/// G7 · Room Recap (optional, opt-in): "You looked at 4 things here", built from the captures in this
/// room. Pick what to keep and save it in one go.
struct RoomRecapSheet: View {
    @Environment(AppState.self) private var app
    @State private var selection: Set<String> = []

    var body: some View {
        let items = app.recapCaptures

        VStack(spacing: 16) {
            Capsule()
                .fill(.white.opacity(0.3))
                .frame(width: 36, height: 5)

            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("You looked at \(items.count) \(items.count == 1 ? "thing" : "things")")
                        .font(.spottedTitle2)
                        .foregroundStyle(.labelPrimary)
                    Text(subtitle(items))
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Button { app.isShowingRoomRecap = false } label: {
                    Icon(name: "icon-close-small", size: 18)
                        .frame(width: 36, height: 36)
                        .background(.white.opacity(0.12), in: Circle())
                        .frame(width: 44, height: 44)
                        .contentShape(Circle())
                }
                .buttonStyle(PressableStyle())
                .accessibilityLabel("Close")
            }

            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, capture in
                    row(capture)
                        .overlay(alignment: .top) {
                            if index > 0 { Rectangle().fill(.white.opacity(0.08)).frame(height: 1) }
                        }
                }
            }
            .background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 20, style: .continuous))

            PrimaryButton(title: "Save \(selection.count) to Saved", height: 56) {
                app.saveRecap(selection)
            }
            .disabled(selection.isEmpty)
            .opacity(selection.isEmpty ? 0.4 : 1)

            Button("Not now") { app.isShowingRoomRecap = false }
                .font(.spottedFootnote)
                .foregroundStyle(.labelPrimary)
                .frame(minHeight: 36)
        }
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .padding(.bottom, 24)
        .glass(in: RoundedRectangle(cornerRadius: 40, style: .continuous), tint: 0.92)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
        .ignoresSafeArea(edges: .bottom)
        .onAppear {
            // Everything not saved yet starts selected.
            selection = Set(items.filter { !app.isSaved($0) }.map(\.id))
        }
    }

    /// "Maya’s apartment · since 4:01 PM"
    private func subtitle(_ items: [Capture]) -> String {
        guard let first = items.map(\.date).min() else { return app.currentRoom }
        return "\(app.currentRoom) · since \(first.formatted(date: .omitted, time: .shortened))"
    }

    private func row(_ capture: Capture) -> some View {
        let isOn = selection.contains(capture.id)
        return Button {
            if isOn { selection.remove(capture.id) } else { selection.insert(capture.id) }
        } label: {
            HStack(spacing: 12) {
                ProductThumbnail(source: capture.image, size: 44, radius: 12)
                VStack(alignment: .leading, spacing: 1) {
                    Text(capture.title)
                        .font(.spottedCallout)
                        .foregroundStyle(.labelPrimary)
                    PriceVerdictLine(price: capture.price, verdict: capture.verdict)
                }
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
                Group {
                    if isOn {
                        Icon(name: "icon-check-on-accent", size: 14)
                            .frame(width: 26, height: 26)
                            .background(.accent, in: Circle())
                    } else {
                        Circle()
                            .strokeBorder(.white.opacity(0.35), lineWidth: 2)
                            .frame(width: 26, height: 26)
                    }
                }
                .frame(width: 44, height: 44)
            }
            .padding(.leading, 10)
            .padding(.trailing, 4)
            .padding(.vertical, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel(capture.title)
        .accessibilityAddTraits(isOn ? .isSelected : [])
    }
}

#Preview("G7 · Room Recap") {
    let app = AppState()
    app.captureMode = .glasses
    app.captures = MockData.captures
    app.isShowingRoomRecap = true
    return RootView().environment(app)
}
