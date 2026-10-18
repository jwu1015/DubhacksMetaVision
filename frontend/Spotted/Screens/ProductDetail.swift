import SwiftUI

/// 03 · Product Detail and 03b · Dupes & Used: hero, Best match / Dupes / Used, price verdict,
/// and a floating add-to-cart bar.
struct ProductDetail: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    var body: some View {
        if let look = app.currentLook, let search = app.currentSearch {
            content(look: look, search: search)
        } else {
            Color.ink.ignoresSafeArea()
        }
    }

    private func content(look: Look, search: SearchResponse) -> some View {
        @Bindable var app = app
        let title = look.name.sentenceCased

        return ZStack(alignment: .bottom) {
            ScrollView {
                VStack(spacing: 0) {
                    hero(image: search.bestMatch?.imageURL ?? look.cutoutURL ?? "")

                    VStack(alignment: .leading, spacing: 16) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text((search.bestMatch?.store ?? look.item?.brandGuess ?? "").uppercased())
                                .font(.spottedCaption)
                                .foregroundStyle(.labelSecondary)
                            Text(title)
                                .font(.spottedTitle1)
                                .foregroundStyle(.labelPrimary)
                            HStack(spacing: 6) {
                                Text((look.item?.style ?? look.label).sentenceCased)
                                    .foregroundStyle(.labelPrimary)
                                Text("· \(app.currentRoom)").foregroundStyle(.labelSecondary)
                            }
                            .font(.spottedFootnote)
                            .lineLimit(1)
                        }

                        SegmentedControl(options: AppState.DetailTab.allCases, title: \.rawValue,
                                         selection: $app.detailTab)

                        tabContent(search: search)
                    }
                    .padding(.horizontal, 20)
                    .padding(.top, 20)
                    .padding(.bottom, 120)
                }
            }
            .scrollIndicators(.hidden)
            .ignoresSafeArea(edges: .top)

            LinearGradient(stops: [
                .init(color: Color(hex: 0x0A0A0D, opacity: 0), location: 0),
                .init(color: Color(hex: 0x0A0A0D, opacity: 0.9), location: 0.6),
                .init(color: Color(hex: 0x0A0A0D), location: 1),
            ], startPoint: .top, endPoint: .bottom)
                .frame(height: 170)
                .ignoresSafeArea(edges: .bottom)
                .allowsHitTesting(false)

            if let match = search.bestMatch {
                bottomBar(match: match, search: search)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 16)
            }
        }
        .overlay(alignment: .top) {
            HStack(spacing: 8) {
                GlassIconButton(icon: "icon-back", label: "Back") { dismiss() }
                Spacer()
                if let url = URL(string: search.bestMatch?.url ?? "") {
                    ShareLink(item: url) {
                        Icon(name: "icon-share")
                            .frame(width: 44, height: 44)
                            .glass(in: Circle(), tint: 0.55)
                    }
                    .accessibilityLabel("Share")
                }
                GlassIconButton(icon: app.isSaved(title) ? "icon-bookmark-fill" : "icon-bookmark",
                                label: app.isSaved(title) ? "Saved. Remove from saved" : "Save") {
                    app.toggleSaveCurrent()
                }
            }
            .padding(.horizontal, 16)
        }
        .background(Color.ink)
    }

    // MARK: - Hero

    /// Full hero on Best match; compact on Dupes and Used so the lists start higher (03b).
    private func hero(image: String) -> some View {
        let isCompact = app.detailTab != .bestMatch

        return ZStack(alignment: .top) {
            LinearGradient(colors: [Color(hex: 0x3D362E), Color(hex: 0x0A0A0D)], startPoint: .top, endPoint: .bottom)
            ProductImage(source: image)
                .frame(width: isCompact ? 161 : 230, height: isCompact ? 137 : 196)
                .padding(.top, isCompact ? 62 : 80)
            Image("page-dots")
                .resizable()
                .frame(width: 42, height: 6)
                .padding(.top, isCompact ? 216 : 300)
                .accessibilityHidden(true)
        }
        .frame(height: isCompact ? 232 : 320)
        .animation(.easeOut(duration: 0.2), value: isCompact)
    }

    // MARK: - Tabs

    @ViewBuilder
    private func tabContent(search: SearchResponse) -> some View {
        switch app.detailTab {
        case .bestMatch:
            if let match = search.bestMatch {
                listingGroup([match])
            } else {
                Text("No exact match for sale")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
            }
            VerdictCard(insight: search.priceInsight, price: search.bestMatch?.price)
        case .dupes:
            if search.isAvailable(.shopping) {
                listingSection(search.dupes, noun: search.dupes.count == 1 ? "similar item" : "similar items",
                               empty: "No similar items found")
            }
        case .used:
            if search.isAvailable(.ebay) {
                listingSection(search.used,
                               noun: search.used.count == 1 ? "pre-owned listing" : "pre-owned listings",
                               empty: "No pre-owned listings found")
            }
        }
        ForEach(ProductSource.searchable.filter { !search.isAvailable($0) }, id: \.self) { source in
            UnavailableSourceCard(source: source, onRetry: app.retry)
        }
    }

    @ViewBuilder
    private func listingSection(_ listings: [Product], noun: String, empty: String) -> some View {
        if let cheapest = listings.map(\.price).min() {
            Text("\(listings.count) \(noun) · from \(Money.short(cheapest))")
                .font(.spottedFootnote)
                .foregroundStyle(.labelSecondary)
            listingGroup(listings)
        } else {
            Text(empty)
                .font(.spottedFootnote)
                .foregroundStyle(.labelSecondary)
        }
    }

    private func listingGroup(_ listings: [Product]) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(listings.enumerated()), id: \.element.id) { index, listing in
                Button {
                    if let url = URL(string: listing.url) { openURL(url) }
                } label: {
                    ListingRow(listing: listing)
                }
                .buttonStyle(PressableStyle())
                .overlay(alignment: .top) {
                    if index > 0 {
                        Rectangle().fill(.white.opacity(0.08)).frame(height: 1)
                    }
                }
            }
        }
        .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }

    // MARK: - Bottom bar

    private func bottomBar(match: Product, search: SearchResponse) -> some View {
        // Compare against every new listing, dupes included, so "lowest new" is never claimed
        // while a cheaper new option is one tab away.
        let newPrices = ([match] + search.dupes).filter { !$0.isUsed }.map(\.price)
        let isLowestNew = !match.isUsed && match.price <= (newPrices.min() ?? match.price)

        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 0) {
                Text(Money.short(match.price))
                    .font(.spottedTitle2.monospacedDigit())
                    .foregroundStyle(.labelPrimary)
                Text(search.priceInsight.bestMatchVerdict.label + (isLowestNew ? " · lowest new" : ""))
                    .font(.spottedCaption)
                    .foregroundStyle(.labelSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            PrimaryButton(title: "Add to cart", icon: "icon-plus-on-accent", height: 52,
                          horizontalPadding: 18, expands: false, action: app.addCurrentToCart)
        }
        .padding(.leading, 20)
        .padding(.trailing, 8)
        .padding(.vertical, 8)
        .glass(in: RoundedRectangle(cornerRadius: 34, style: .continuous))
    }
}

/// One store result: name, match tag, source/condition, price.
struct ListingRow: View {
    let listing: Product

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(listing.store)
                        .font(.spottedHeadline)
                        .foregroundStyle(.labelPrimary)
                    MatchTag(product: listing)
                }
                HStack(spacing: 5) {
                    Icon(name: icon, size: 13)
                    Text(subtitle)
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                        .lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(Money.short(listing.price))
                .font(.spottedHeadline.monospacedDigit())
                .foregroundStyle(.labelPrimary)
            Icon(name: "icon-chevron", size: 16)
        }
        .padding(.leading, 16)
        .padding(.trailing, 12)
        .padding(.vertical, 14)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private var icon: String {
        if listing.isUsed { return "icon-pre-owned" }
        return listing.source == .lens ? "icon-lens" : "icon-tag"
    }

    /// e.g. "Google Lens · New · 92% match", "Google Shopping · New · Save 62%", "eBay · Pre-owned".
    private var subtitle: String {
        var parts = [listing.source.displayName, listing.isUsed ? "Pre-owned" : "New"]
        if let score = listing.matchScore {
            parts.append("\(score)% match")
        } else if let savings = listing.savingsPct {
            parts.append("Save \(savings)%")
        }
        return parts.joined(separator: " · ")
    }
}

/// Price verdict with the typical range (p25–p75), median and this price.
/// The range can be missing (too few comparable prices), so the bar only shows when it's complete.
struct VerdictCard: View {
    let insight: PriceInsight
    let price: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VerdictChip(verdict: insight.bestMatchVerdict, detailed: true)
                Spacer()
                if let median = insight.median {
                    Text("Median \(Money.short(median))")
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                }
            }
            if let low = insight.typicalLow, let high = insight.typicalHigh, let median = insight.median,
               let price, high > low {
                PriceRangeBar(low: low, high: high, median: median, price: price)
            }
            Text(insight.verdictText)
                .font(.spottedFootnote)
                .foregroundStyle(.labelSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

struct PriceRangeBar: View {
    /// Typical range: p25 and p75.
    let low: Double
    let high: Double
    let median: Double
    let price: Double

    var body: some View {
        GeometryReader { geo in
            let span = high - low
            let start = min(low, price) - span * 0.45
            let end = max(high, price) + span * 0.35
            let x = { (value: Double) -> CGFloat in CGFloat((value - start) / (end - start)) * geo.size.width }

            ZStack(alignment: .topLeading) {
                Capsule().fill(.white.opacity(0.1))
                    .frame(width: geo.size.width, height: 6)
                    .offset(y: 22)
                Capsule().fill(.white.opacity(0.32))
                    .frame(width: x(high) - x(low), height: 6)
                    .offset(x: x(low), y: 22)
                RoundedRectangle(cornerRadius: 1).fill(.white.opacity(0.85))
                    .frame(width: 2, height: 14)
                    .offset(x: x(median) - 1, y: 18)
                Image("range-this-price")
                    .resizable()
                    .frame(width: 18, height: 18)
                    .offset(x: x(price) - 9, y: 16)

                Text(Money.short(price))
                    .font(.spottedCaption)
                    .foregroundStyle(.labelPrimary)
                    .fixedSize()
                    .position(x: x(price), y: 8)

                Group {
                    Text(Money.short(low)).position(x: x(low), y: 52)
                    Text("typical range").position(x: (x(low) + x(high)) / 2, y: 52)
                    Text(Money.short(high)).position(x: x(high), y: 52)
                }
                .font(.spottedCaption)
                .foregroundStyle(.labelTertiary)
                .fixedSize()
            }
        }
        .frame(height: 60)
        .accessibilityElement()
        .accessibilityLabel("Typical range \(Money.short(low)) to \(Money.short(high)), median \(Money.short(median)). This price \(Money.short(price)).")
    }
}

/// Partial results: a source that timed out shows as unavailable, never as empty.
struct UnavailableSourceCard: View {
    let source: ProductSource
    let onRetry: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Icon(name: "icon-timeout", size: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(source.displayName)
                    .font(.spottedCallout)
                    .foregroundStyle(.labelPrimary)
                Text("Unavailable right now")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: onRetry) {
                HStack(spacing: 6) {
                    Icon(name: "icon-retry", size: 16)
                    Text("Retry")
                        .font(.spottedCallout)
                        .foregroundStyle(.labelPrimary)
                }
                .padding(.leading, 14)
                .padding(.trailing, 16)
                .frame(height: 44)
                .background(.white.opacity(0.12), in: Capsule())
            }
            .buttonStyle(PressableStyle())
            .accessibilityLabel("Retry \(source.displayName)")
        }
        .padding(.leading, 16)
        .padding(.trailing, 12)
        .padding(.vertical, 12)
        .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .strokeBorder(.white.opacity(0.1), style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
        )
    }
}

#Preview("03 · Product Detail") {
    let app = AppState()
    app.selectedTrackID = MockData.chair.trackID
    app.scan = .matched(Look(MockData.detect).finished(with: MockData.search), MockData.search)
    return ProductDetail().environment(app)
}

