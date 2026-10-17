import SwiftUI

/// 03 · Product Detail and 03b · Dupes & Used: hero, Best match / Dupes / Used, price verdict,
/// and a floating add-to-cart bar.
struct ProductDetail: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    var body: some View {
        if let detect = app.currentDetect, let search = app.currentSearch {
            content(detect: detect, search: search)
        } else {
            Color.ink.ignoresSafeArea()
        }
    }

    private func content(detect: DetectResponse, search: SearchResponse) -> some View {
        @Bindable var app = app
        let title = search.bestMatch?.title ?? detect.category.sentenceCased

        return ZStack(alignment: .bottom) {
            ScrollView {
                VStack(spacing: 0) {
                    hero(image: search.bestMatch?.thumbnail ?? detect.cutoutURL)

                    VStack(alignment: .leading, spacing: 16) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text((search.bestMatch?.store ?? detect.brandGuess ?? "").uppercased())
                                .font(.spottedCaption)
                                .foregroundStyle(.labelSecondary)
                            Text(title)
                                .font(.spottedTitle1)
                                .foregroundStyle(.labelPrimary)
                            HStack(spacing: 6) {
                                Text(detect.style.sentenceCased).foregroundStyle(.labelPrimary)
                                Text("· \(detect.description) · \(app.currentRoom)").foregroundStyle(.labelSecondary)
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
            }
            VerdictCard(pricing: search.pricing, price: search.bestMatch?.price ?? search.pricing.median)
        case .dupes:
            if search.isAvailable(.googleShopping) {
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
        ForEach(Source.allCases.filter { !search.isAvailable($0) }, id: \.self) { source in
            UnavailableSourceCard(source: source, onRetry: app.retrySearch)
        }
    }

    @ViewBuilder
    private func listingSection(_ listings: [Listing], noun: String, empty: String) -> some View {
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

    private func listingGroup(_ listings: [Listing]) -> some View {
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

    private func bottomBar(match: Listing, search: SearchResponse) -> some View {
        let newPrices = ([match] + search.dupes).filter { $0.condition == "new" && $0.isExactMatch }.map(\.price)
        let isLowestNew = match.condition == "new" && match.price <= (newPrices.min() ?? match.price)

        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 0) {
                Text(Money.short(match.price))
                    .font(.spottedTitle2.monospacedDigit())
                    .foregroundStyle(.labelPrimary)
                Text(search.pricing.verdict.label + (isLowestNew ? " · lowest new" : ""))
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
    let listing: Listing

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(listing.store)
                        .font(.spottedHeadline)
                        .foregroundStyle(.labelPrimary)
                    MatchTag(isExact: listing.isExactMatch)
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

    private var isUsed: Bool { listing.condition != "new" }

    private var icon: String {
        if isUsed { return "icon-pre-owned" }
        return listing.source == Source.googleLens.rawValue ? "icon-lens" : "icon-tag"
    }

    private var subtitle: String {
        if isUsed { return "Pre-owned · \(listing.similarity)% match" }
        let source = listing.source.flatMap(Source.init(rawValue:))?.name ?? listing.store
        return "\(source) · New · \(listing.similarity)% match"
    }
}

/// Price verdict with the typical range (p25–p75), median and this price.
struct VerdictCard: View {
    let pricing: Pricing
    let price: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VerdictChip(verdict: pricing.verdict, detailed: true)
                Spacer()
                Text("Median \(Money.short(pricing.median))")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
            }
            PriceRangeBar(pricing: pricing, price: price)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

struct PriceRangeBar: View {
    let pricing: Pricing
    let price: Double

    var body: some View {
        GeometryReader { geo in
            let span = pricing.p75 - pricing.p25
            let low = min(pricing.p25, price) - span * 0.45
            let high = max(pricing.p75, price) + span * 0.35
            let x = { (value: Double) -> CGFloat in CGFloat((value - low) / (high - low)) * geo.size.width }

            ZStack(alignment: .topLeading) {
                Capsule().fill(.white.opacity(0.1))
                    .frame(width: geo.size.width, height: 6)
                    .offset(y: 22)
                Capsule().fill(.white.opacity(0.32))
                    .frame(width: x(pricing.p75) - x(pricing.p25), height: 6)
                    .offset(x: x(pricing.p25), y: 22)
                RoundedRectangle(cornerRadius: 1).fill(.white.opacity(0.85))
                    .frame(width: 2, height: 14)
                    .offset(x: x(pricing.median) - 1, y: 18)
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
                    Text(Money.short(pricing.p25)).position(x: x(pricing.p25), y: 52)
                    Text("typical range").position(x: (x(pricing.p25) + x(pricing.p75)) / 2, y: 52)
                    Text(Money.short(pricing.p75)).position(x: x(pricing.p75), y: 52)
                }
                .font(.spottedCaption)
                .foregroundStyle(.labelTertiary)
                .fixedSize()
            }
        }
        .frame(height: 60)
        .accessibilityElement()
        .accessibilityLabel("Typical range \(Money.short(pricing.p25)) to \(Money.short(pricing.p75)), median \(Money.short(pricing.median)). This price \(Money.short(price)).")
    }
}

/// Partial results: a source that timed out shows as unavailable, never as empty.
struct UnavailableSourceCard: View {
    let source: Source
    let onRetry: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Icon(name: "icon-timeout", size: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(source.name)
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
            .accessibilityLabel("Retry \(source.name)")
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
    app.show(.matched)
    return ProductDetail().environment(app)
}

#Preview("03b · Dupes & Used") {
    let app = AppState()
    app.show(.usedTimeout)
    return ProductDetail().environment(app)
}
