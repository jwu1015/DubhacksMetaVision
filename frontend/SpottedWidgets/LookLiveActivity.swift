import ActivityKit
import SwiftUI
import WidgetKit

// The widget extension can't see the app's Tokens.swift, so it carries the few colors it needs.
private extension Color {
    static let lime = Color(red: 0xC8 / 255, green: 0xFF / 255, blue: 0x3D / 255)
    static let onLime = Color(red: 0x0A / 255, green: 0x0A / 255, blue: 0x0C / 255)
    static let raised = Color(red: 0x22 / 255, green: 0x22 / 255, blue: 0x28 / 255)
    static let secondaryLabel = Color(red: 0xA1 / 255, green: 0xA1 / 255, blue: 0xAA / 255)
    static let success = Color(red: 0x30 / 255, green: 0xD1 / 255, blue: 0x58 / 255)
    static let warning = Color(red: 0xFF / 255, green: 0xD6 / 255, blue: 0x0A / 255)
}

/// G6 · Lock screen card and Dynamic Island for a Look. Buttons deep-link into the app, which
/// does the save or add (and asks for Face ID before anything involving money).
struct LookLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: LookActivityAttributes.self) { context in
            LockScreenCard(state: context.state, captureID: context.attributes.captureID)
                .activityBackgroundTint(Color(red: 0x16 / 255, green: 0x16 / 255, blue: 0x1A / 255).opacity(0.78))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            let state = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Thumbnail(asset: state.artAsset, size: 44)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(state.title).font(.headline).lineLimit(1)
                        Summary(state: state)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Actions(captureID: context.attributes.captureID, isSearching: state.isSearching)
                }
            } compactLeading: {
                Thumbnail(asset: state.artAsset, size: 26)
            } compactTrailing: {
                Text(state.isSearching ? "Finding…" : [state.price, state.verdict.map(shortVerdict)].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption.weight(.medium))
                    .foregroundStyle(Color.lime)
            } minimal: {
                Thumbnail(asset: state.artAsset, size: 24)
            }
            .keylineTint(.lime)
        }
    }
}

private func shortVerdict(_ verdict: String) -> String {
    verdict.replacingOccurrences(of: " price", with: "")
}

private struct LockScreenCard: View {
    let state: LookActivityAttributes.ContentState
    let captureID: String

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Thumbnail(asset: state.artAsset, size: 52)
                VStack(alignment: .leading, spacing: 2) {
                    Text(state.title)
                        .font(.headline)
                        .foregroundStyle(.white)
                        .lineLimit(1)
                    Summary(state: state)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Text("Storefront")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(Color.lime)
            }
            Actions(captureID: captureID, isSearching: state.isSearching)
        }
        .padding(16)
    }
}

/// "$189 · Fair price" with the verdict in its color, or "Finding prices…" while searching.
private struct Summary: View {
    let state: LookActivityAttributes.ContentState

    var body: some View {
        if let price = state.price, let verdict = state.verdict {
            (Text("\(price) · ").foregroundColor(.secondaryLabel) + Text(verdict).foregroundColor(color(verdict)))
                .font(.footnote)
        } else {
            Text("Finding prices…")
                .font(.footnote)
                .foregroundStyle(Color.secondaryLabel)
        }
    }

    private func color(_ verdict: String) -> Color {
        switch verdict {
        case "Low price": .success
        case "High price": .warning
        default: .secondaryLabel
        }
    }
}

private struct Actions: View {
    let captureID: String
    let isSearching: Bool

    var body: some View {
        HStack(spacing: 8) {
            Link(destination: LookLink.save(captureID)) {
                HStack(spacing: 6) {
                    Image("icon-bookmark").resizable().frame(width: 20, height: 20)
                    Text("Save").font(.headline)
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(.white.opacity(0.12), in: Capsule())
            }
            Link(destination: LookLink.add(captureID)) {
                HStack(spacing: 6) {
                    Image("icon-plus-on-accent").resizable().frame(width: 20, height: 20)
                    Text("Add to cart").font(.headline)
                }
                .foregroundStyle(Color.onLime)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(Color.lime, in: Capsule())
            }
            .disabled(isSearching)
            .opacity(isSearching ? 0.4 : 1)
            Link(destination: LookLink.talk) {
                Image("icon-mic").resizable().frame(width: 22, height: 22)
                    .frame(width: 44, height: 44)
                    .background(.white.opacity(0.12), in: Circle())
                    .overlay(Circle().strokeBorder(.white.opacity(0.18), lineWidth: 1))
            }
            .accessibilityLabel("Talk to Storefront")
        }
    }
}

private struct Thumbnail: View {
    let asset: String?
    let size: CGFloat

    var body: some View {
        Group {
            if let asset {
                Image(asset).resizable().scaledToFit()
                    .frame(width: size * 0.875, height: size * 0.75)
            } else {
                Image("icon-glasses-accent").resizable().scaledToFit()
                    .frame(width: size * 0.5, height: size * 0.5)
            }
        }
        .frame(width: size, height: size)
        .background(Color.raised, in: RoundedRectangle(cornerRadius: size * 0.27, style: .continuous))
        .accessibilityHidden(true)
    }
}

// MARK: - Previews (lock screen and Dynamic Island in Xcode's canvas)

private let previewAttributes = LookActivityAttributes(captureID: "cap_lamp")
private let searching = LookActivityAttributes.ContentState(title: "Lamp", price: nil, verdict: nil, artAsset: "art-arc-lamp")
private let result = LookActivityAttributes.ContentState(title: "Brass arc lamp", price: "$189", verdict: "Fair price",
                                                         artAsset: "art-arc-lamp")

#Preview("Lock screen", as: .content, using: previewAttributes) {
    LookLiveActivity()
} contentStates: {
    searching
    result
}

#Preview("Dynamic Island · compact", as: .dynamicIsland(.compact), using: previewAttributes) {
    LookLiveActivity()
} contentStates: {
    searching
    result
}

#Preview("Dynamic Island · expanded", as: .dynamicIsland(.expanded), using: previewAttributes) {
    LookLiveActivity()
} contentStates: {
    result
}
