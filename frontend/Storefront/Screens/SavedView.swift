import SwiftUI

/// 04 · Saved: things you spotted, grouped by room.
struct SavedView: View {
    @Environment(AppState.self) private var app
    @State private var segment = Segment.saved

    enum Segment: String, CaseIterable {
        case saved = "Saved", history = "History"
    }

    var body: some View {
        ZStack(alignment: .top) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Saved")
                            .font(.storefrontLargeTitle)
                            .foregroundStyle(.labelPrimary)
                        Text(app.saved.isEmpty
                             ? "Nothing saved yet"
                             : "\(app.saved.count) \(app.saved.count == 1 ? "thing" : "things") you spotted this week")
                            .font(.storefrontSubhead)
                            .foregroundStyle(.labelSecondary)
                    }

                    SegmentedControl(options: Segment.allCases, title: \.rawValue, selection: $segment)

                    if segment == .saved, app.saved.isEmpty {
                        Text("Tap the bookmark on anything you spot to keep it here.")
                            .font(.storefrontSubhead)
                            .foregroundStyle(.labelSecondary)
                    } else if segment == .saved {
                        ForEach(app.rooms) { room in
                            let items = app.items(in: room)
                            if !items.isEmpty {
                                RoomSection(room: room, items: items)
                            }
                        }
                    } else {
                        // History isn't designed yet.
                        Text("Everything you spot shows up here, even if you don't save it.")
                            .font(.storefrontSubhead)
                            .foregroundStyle(.labelSecondary)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 42)
                .padding(.bottom, 110)
            }
            .scrollIndicators(.hidden)

            // Search and More aren't designed yet.
            HStack(spacing: 8) {
                Spacer()
                GlassIconButton(icon: "icon-search", label: "Search saved") {}
                GlassIconButton(icon: "icon-more", label: "More") {}
            }
            .padding(.horizontal, 16)
        }
        .overlay(alignment: .bottom) { ScrollEdgeFade() }
        .background(Color.ink)
    }
}

private struct RoomSection: View {
    @Environment(AppState.self) private var app
    let room: Room
    let items: [SavedItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            RoomHeader(name: room.name, subtitle: "\(room.visited) · \(items.count) spotted")

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                ForEach(items) { item in
                    SavedCard(item: item)
                        .contextMenu {
                            Button("Remove from saved", role: .destructive) {
                                app.saved.removeAll { $0.id == item.id }
                            }
                        }
                }
            }
        }
    }
}

private struct SavedCard: View {
    let item: SavedItem

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                Color.surfaceRaised
                ProductImage(source: item.image)
                    .frame(width: 118, height: 100)
                    .offset(y: 4)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                SavedBadge()
                    .padding(8)
            }
            .frame(height: 124)

            VStack(alignment: .leading, spacing: 2) {
                Text(item.title)
                    .font(.storefrontCallout)
                    .foregroundStyle(.labelPrimary)
                    .lineLimit(1)
                Text(item.store)
                    .font(.storefrontFootnote)
                    .foregroundStyle(.labelSecondary)
                    .lineLimit(1)
                Text(Money.short(item.price))
                    .font(.storefrontHeadline.monospacedDigit())
                    .foregroundStyle(.labelPrimary)
            }
            .padding(.horizontal, 12)
            .padding(.top, 10)
            .padding(.bottom, 12)
        }
        .background(.surface)
        .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

/// Saved = coral filled bookmark + "Saved" label.
private struct SavedBadge: View {
    var body: some View {
        HStack(spacing: 4) {
            Icon(name: "icon-bookmark-fill", size: 12)
            Text("Saved")
                .font(.storefrontCaption)
                .foregroundStyle(.labelPrimary)
        }
        .padding(.leading, 7)
        .padding(.trailing, 9)
        .padding(.vertical, 4)
        .background(Color(hex: 0x0A0A0D, opacity: 0.6), in: Capsule())
    }
}

/// Fades content out under the floating tab bar.
struct ScrollEdgeFade: View {
    var body: some View {
        LinearGradient(colors: [Color(hex: 0x0A0A0D, opacity: 0), Color(hex: 0x0A0A0D, opacity: 0.9)],
                       startPoint: .top, endPoint: .bottom)
            .frame(height: 140)
            .ignoresSafeArea(edges: .bottom)
            .allowsHitTesting(false)
    }
}

#Preview("04 · Saved") {
    let app = AppState()
    app.saved = MockData.saved
    app.tab = .saved
    return RootView().environment(app)
}
