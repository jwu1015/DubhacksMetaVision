import SwiftUI

/// G5 · Spotted Today: every capture the user asked for, grouped by room. It doubles as history and
/// as privacy proof. The Saved tab shows it in glasses mode.
struct SpottedTodayView: View {
    @Environment(AppState.self) private var app
    @State private var range = Range.today

    enum Range: String, CaseIterable {
        case today = "Today", week = "This week"
    }

    var body: some View {
        ZStack(alignment: .top) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(range == .today ? "Spotted today" : "Spotted this week")
                            .font(.spottedLargeTitle)
                            .foregroundStyle(.labelPrimary)
                        Text("\(captures.count) \(captures.count == 1 ? "capture" : "captures") · only the ones you asked for")
                            .font(.spottedSubhead)
                            .foregroundStyle(.labelSecondary)
                    }

                    SegmentedControl(options: Range.allCases, title: \.rawValue, selection: $range)

                    if captures.isEmpty {
                        Text("Nothing yet. Say “Spotted” and ask about something you see.")
                            .font(.spottedSubhead)
                            .foregroundStyle(.labelSecondary)
                    }

                    ForEach(rooms, id: \.self) { room in
                        let items = captures.filter { $0.room == room }
                        VStack(alignment: .leading, spacing: 10) {
                            RoomHeader(name: room, subtitle: subtitle(for: items))
                            VStack(spacing: 0) {
                                ForEach(Array(items.enumerated()), id: \.element.id) { index, capture in
                                    CaptureRow(capture: capture)
                                        .overlay(alignment: .top) {
                                            if index > 0 { Rectangle().fill(.white.opacity(0.08)).frame(height: 1) }
                                        }
                                }
                            }
                            .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                        }
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
                GlassIconButton(icon: "icon-search", label: "Search captures") {}
                GlassIconButton(icon: "icon-more", label: "More") {}
            }
            .padding(.horizontal, 16)
        }
        .overlay(alignment: .bottom) { ScrollEdgeFade() }
        .background(Color.ink)
    }

    private var captures: [Capture] {
        let calendar = Calendar.current
        return app.captures.filter { capture in
            switch range {
            case .today: calendar.isDateInToday(capture.date)
            case .week: capture.date > calendar.date(byAdding: .day, value: -7, to: .now) ?? .distantPast
            }
        }
    }

    /// Rooms in the order they were last visited.
    private var rooms: [String] {
        var seen = Set<String>()
        return captures.map(\.room).filter { seen.insert($0).inserted }
    }

    /// "Today · 4:12 PM · 3 spotted"
    private func subtitle(for items: [Capture]) -> String {
        guard let latest = items.map(\.date).max() else { return "" }
        let day = Calendar.current.isDateInToday(latest) ? "Today"
            : Calendar.current.isDateInYesterday(latest) ? "Yesterday"
            : latest.formatted(.dateTime.weekday(.wide))
        return "\(day) · \(latest.formatted(date: .omitted, time: .shortened)) · \(items.count) spotted"
    }
}

/// One capture with Save and Add.
private struct CaptureRow: View {
    @Environment(AppState.self) private var app
    let capture: Capture

    var body: some View {
        HStack(spacing: 12) {
            ProductThumbnail(source: capture.image, size: 52, radius: 14)
            VStack(alignment: .leading, spacing: 2) {
                Text(capture.title)
                    .font(.spottedCallout)
                    .foregroundStyle(.labelPrimary)
                PriceVerdictLine(price: capture.price, verdict: capture.verdict)
            }
            .lineLimit(1)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)

            let isSaved = app.isSaved(capture)
            CircleButton(icon: isSaved ? "icon-bookmark-fill" : "icon-bookmark", size: 40,
                         label: isSaved ? "Saved. Remove from saved" : "Save \(capture.title)") {
                app.toggleSave(capture)
            }
            Button {
                if let product = capture.product { Task { await app.add(product) } }
            } label: {
                Icon(name: "icon-plus-on-accent", size: 20)
                    .frame(width: 40, height: 40)
                    .background(.accent, in: Circle())
            }
            .buttonStyle(PressableStyle())
            .disabled(capture.product == nil)
            .opacity(capture.product == nil ? 0.4 : 1)
            .accessibilityLabel("Add \(capture.title) to cart")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }
}

#Preview("G5 · Spotted Today") {
    let app = AppState()
    app.captureMode = .glasses
    app.captures = MockData.captures
    app.tab = .saved
    return RootView().environment(app)
}
