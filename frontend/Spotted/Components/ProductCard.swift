import SwiftUI

private let cardShape = RoundedRectangle(cornerRadius: 32, style: .continuous)

/// 01 · Matched product card: best match, price, verdict and actions.
struct ProductCard: View {
    let look: Look
    let search: SearchResponse
    let isSaved: Bool
    let onOpen: () -> Void
    let onSave: () -> Void
    let onAdd: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button(action: onOpen) { header }
                .buttonStyle(.plain)
                .accessibilityHint("Opens matches and prices")

            HStack(spacing: 8) {
                VerdictChip(verdict: search.priceInsight.bestMatchVerdict)
                if let low = search.priceInsight.typicalLow, let high = search.priceInsight.typicalHigh {
                    Text("typical \(Money.short(low))–\(Money.short(high))")
                        .font(.spottedCaption)
                        .foregroundStyle(.labelSecondary)
                }
            }

            HStack(spacing: 8) {
                if let match = search.bestMatch {
                    Chip(title: match.matchLabel,
                         icon: match.isExactMatch ? "icon-check-chip" : nil)
                }
                if let cheapestUsed = search.used.map(\.price).min() {
                    Chip(title: "Used from \(Money.short(cheapestUsed))", icon: "icon-tag")
                }
            }

            HStack(spacing: 8) {
                CircleButton(icon: "icon-search", label: "See all matches", action: onOpen)
                SaveButton(isSaved: isSaved, action: onSave)
                PrimaryButton(title: "Add to cart", icon: "icon-plus-on-accent", action: onAdd)
            }
        }
        .padding(16)
        .glass(in: cardShape)
    }

    private var header: some View {
        HStack(spacing: 12) {
            ProductThumbnail(source: search.bestMatch?.imageURL ?? look.cutoutURL ?? "")
            VStack(alignment: .leading, spacing: 2) {
                // Claude's name for the item (SearchResponse.item), not the store's listing title.
                Text(look.name.sentenceCased)
                    .font(.spottedHeadline)
                    .foregroundStyle(.labelPrimary)
                Text(search.bestMatch.map { "\($0.store) · \(search.storeCount) stores" } ?? "No exact match for sale")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelSecondary)
            }
            .lineLimit(1)
            .frame(maxWidth: .infinity, alignment: .leading)
            if let price = search.bestMatch?.price {
                Text(Money.short(price))
                    .font(.spottedTitle2.monospacedDigit())
                    .foregroundStyle(.labelPrimary)
            }
        }
        .contentShape(Rectangle())
    }
}

/// 01b · After /detect: cutout, quick phrase and skeleton prices while /search runs.
struct SearchingCard: View {
    let look: Look
    let finishedSources: Set<ProductSource>
    let isSaved: Bool
    let onSave: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                // The cutout is a crop of the photo, so it keeps the default surface background.
                ProductThumbnail(source: look.cutoutURL ?? "")
                VStack(alignment: .leading, spacing: 2) {
                    Text(look.name.sentenceCased)
                        .font(.spottedHeadline)
                        .foregroundStyle(.labelPrimary)
                    // Empty until the Look's "described" stage arrives with Claude's item.
                    Text(look.item?.description.flatMap { $0.isEmpty ? nil : $0 } ?? "Finding prices")
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                }
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            GlassesSaidRow(text: look.quickPhrase)

            VStack(spacing: 6) {
                skeletonRow(store: 150, meta: 88)
                skeletonRow(store: 118, meta: 64)
                HStack {
                    Text("Checking \(ProductSource.searchable.count) stores…")
                        .font(.spottedCaption)
                        .foregroundStyle(.labelSecondary)
                    Spacer(minLength: 8)
                    HStack(spacing: 10) {
                        ForEach(ProductSource.searchable, id: \.self) { source in
                            let isDone = finishedSources.contains(source)
                            HStack(spacing: 4) {
                                Icon(name: isDone ? "dot-done" : "dot-checking", size: 6)
                                Text(source.shortName)
                                    .font(.spottedCaption)
                                    .foregroundStyle(isDone ? Color.labelSecondary : Color.labelTertiary)
                            }
                        }
                    }
                }
                .accessibilityElement(children: .combine)
            }

            HStack(spacing: 8) {
                CircleButton(icon: "icon-search", label: "See all matches") {}
                    .disabled(true)
                    .opacity(0.4)
                SaveButton(isSaved: isSaved, action: onSave)
                PrimaryButton(title: "Add to cart", icon: "icon-plus-on-accent") {}
                    .disabled(true)
                    .opacity(0.4)
            }
        }
        .padding(16)
        .glass(in: cardShape)
    }

    private func skeletonRow(store: CGFloat, meta: CGFloat) -> some View {
        HStack {
            HStack(spacing: 8) {
                ShimmerBar(width: store)
                ShimmerBar(width: meta)
            }
            Spacer()
            ShimmerBar(width: 56)
        }
        .accessibilityHidden(true)
    }
}

/// 01c · /detect failed or came back low confidence with nothing to offer.
struct CouldntTellCard: View {
    let spokenLine: String
    let onRetry: () -> Void
    let onTakePhoto: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Icon(name: "icon-question", size: 22)
                    .frame(width: 44, height: 44)
                    .background(.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text("Couldn't tell what that is")
                        .font(.spottedHeadline)
                        .foregroundStyle(.labelPrimary)
                    Text("Try moving closer or pointing at one item.")
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            GlassesSaidRow(text: spokenLine)
            HStack(spacing: 8) {
                PrimaryButton(title: "Try again", icon: "icon-retry-on-accent", action: onRetry)
                SecondaryButton(title: "Take a photo", icon: "icon-camera", action: onTakePhoto)
            }
        }
        .padding(16)
        .glass(in: cardShape)
    }
}

/// Save is instant and undoable. Saved = coral filled bookmark, never color alone.
struct SaveButton: View {
    let isSaved: Bool
    let action: () -> Void

    var body: some View {
        CircleButton(icon: isSaved ? "icon-bookmark-fill" : "icon-bookmark",
                     label: isSaved ? "Saved. Remove from saved" : "Save", action: action)
            .sensoryFeedback(.success, trigger: isSaved)
    }
}

/// Skeleton placeholder with a soft moving highlight (static under Reduce Motion).
struct ShimmerBar: View {
    let width: CGFloat

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var phase: CGFloat = -1

    var body: some View {
        RoundedRectangle(cornerRadius: 4)
            .fill(LinearGradient(
                stops: [
                    .init(color: .white.opacity(0.08), location: 0),
                    .init(color: .white.opacity(0.18), location: 0.5),
                    .init(color: .white.opacity(0.08), location: 1),
                ],
                startPoint: UnitPoint(x: phase, y: 0.5),
                endPoint: UnitPoint(x: phase + 1, y: 0.5)
            ))
            .frame(width: width, height: 8)
            .onAppear {
                guard !reduceMotion else { phase = 0; return }
                withAnimation(.linear(duration: 1.4).repeatForever(autoreverses: false)) { phase = 1 }
            }
    }
}
