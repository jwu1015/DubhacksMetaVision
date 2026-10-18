import SwiftUI

/// 05 · Cart: line items, the Visa agent limit, summary and Check out.
struct CartView: View {
    @Environment(AppState.self) private var app
    @State private var pendingRemoval: CartItem?

    var body: some View {
        ZStack(alignment: .top) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Cart")
                            .font(.spottedLargeTitle)
                            .foregroundStyle(.labelPrimary)
                        Text(subtitle)
                            .font(.spottedSubhead)
                            .foregroundStyle(.labelSecondary)
                    }

                    if !app.cart.isEmpty {
                        items
                        AgentLimitCard()
                        summary
                        PrimaryButton(title: "Check out · \(Money.exact(app.total))", height: 56) {
                            app.isShowingCheckout = true
                        }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 42)
                .padding(.bottom, 110)
            }
            .scrollIndicators(.hidden)

            // More isn't designed yet.
            HStack {
                Spacer()
                GlassIconButton(icon: "icon-more", label: "More") {}
            }
            .padding(.horizontal, 16)
        }
        .overlay(alignment: .bottom) { ScrollEdgeFade() }
        .background(Color.ink)
        .confirmationDialog("Remove \(pendingRemoval?.title ?? "item")?",
                            isPresented: Binding(get: { pendingRemoval != nil }, set: { if !$0 { pendingRemoval = nil } }),
                            titleVisibility: .visible) {
            Button("Remove", role: .destructive) {
                if let item = pendingRemoval { app.changeQuantity(of: item, by: -1) }
                pendingRemoval = nil
            }
        }
    }

    private var subtitle: String {
        let count = app.cart.reduce(0) { $0 + $1.quantity }
        if count == 0 { return "Nothing here yet" }
        return "\(count) \(count == 1 ? "item" : "items") · ready to check out"
    }

    private var items: some View {
        VStack(spacing: 0) {
            ForEach(Array(app.cart.enumerated()), id: \.element.id) { index, item in
                HStack(spacing: 12) {
                    ProductThumbnail(source: item.image, size: 64)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.title)
                            .font(.spottedCallout)
                            .foregroundStyle(.labelPrimary)
                        Text("\(item.store) · \(item.room)")
                            .font(.spottedCaption)
                            .foregroundStyle(.labelSecondary)
                        QuantityStepper(quantity: item.quantity) { delta in
                            // Removing is destructive, so it asks first.
                            if item.quantity + delta <= 0 {
                                pendingRemoval = item
                            } else {
                                app.changeQuantity(of: item, by: delta)
                            }
                        }
                    }
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    Text(Money.short(item.price * Double(item.quantity)))
                        .font(.spottedHeadline.monospacedDigit())
                        .foregroundStyle(.labelPrimary)
                }
                .padding(.leading, 12)
                .padding(.trailing, 14)
                .padding(.vertical, 12)
                .overlay(alignment: .top) {
                    if index > 0 {
                        Rectangle().fill(Color.separator).frame(height: 1)
                    }
                }
            }
        }
        .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }

    private var summary: some View {
        VStack(spacing: 10) {
            summaryRow("Subtotal", Money.exact(app.subtotal))
            summaryRow("Shipping", "Free")
            summaryRow("Est. tax (WA)", Money.exact(app.tax))
            HStack {
                Text("Total")
                    .font(.spottedHeadline)
                Spacer()
                Text(Money.exact(app.total))
                    .font(.spottedTitle2.monospacedDigit())
            }
            .foregroundStyle(.labelPrimary)
            .accessibilityElement(children: .combine)
        }
        .padding(.horizontal, 4)
    }

    private func summaryRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).foregroundStyle(.labelSecondary)
            Spacer()
            Text(value).foregroundStyle(.labelPrimary)
        }
        .font(.spottedSubhead)
        .accessibilityElement(children: .combine)
    }
}

private struct QuantityStepper: View {
    let quantity: Int
    let onChange: (Int) -> Void

    var body: some View {
        HStack(spacing: 0) {
            Button { onChange(-1) } label: {
                Icon(name: "icon-minus", size: 14).frame(width: 32, height: 28)
            }
            Text("\(quantity)")
                .font(.spottedFootnote)
                .foregroundStyle(.labelPrimary)
                .frame(width: 32, height: 28)
            Button { onChange(1) } label: {
                Icon(name: "icon-plus", size: 14).frame(width: 32, height: 28)
            }
        }
        .buttonStyle(PressableStyle())
        .background(.white.opacity(0.08), in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Quantity")
        .accessibilityValue("\(quantity)")
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: onChange(1)
            case .decrement: onChange(-1)
            @unknown default: break
            }
        }
    }
}

/// The Visa "agent limit": how much the assistant may spend, enforced again on the backend.
struct AgentLimitCard: View {
    @Environment(AppState.self) private var app

    var body: some View {
        let fraction = min(app.total / app.agentLimit.amount, 1)

        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                VisaMark()
                VStack(alignment: .leading, spacing: 1) {
                    Text("Agent limit")
                        .font(.spottedHeadline)
                        .foregroundStyle(.labelPrimary)
                    Text("Visa ••\(app.agentLimit.cardLast4) · \(app.merchants)")
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                // Editing the limit isn't designed yet (it needs a confirm step).
                Text("Edit")
                    .font(.spottedCallout)
                    .foregroundStyle(.labelPrimary)
            }

            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(.white.opacity(0.12))
                    Capsule().fill(app.isWithinLimit ? Color.accent : Color.error)
                        .frame(width: geo.size.width * fraction)
                }
            }
            .frame(height: 8)

            HStack(spacing: 8) {
                Icon(name: "icon-shield", size: 16)
                Text(app.isWithinLimit
                     ? "\(Money.exact(app.total)) of \(Money.short(app.agentLimit.amount)) · Face ID required"
                     : "Over your \(Money.short(app.agentLimit.amount)) limit · approve on your phone")
                    .font(.spottedFootnote)
                    .foregroundStyle(app.isWithinLimit ? Color.labelSecondary : Color.error)
            }
        }
        .padding(16)
        .background(
            LinearGradient(colors: [Color(hex: 0x1434CB, opacity: 0.35), .surface], startPoint: .leading, endPoint: .trailing),
            in: RoundedRectangle(cornerRadius: 22, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .strokeBorder(Color(hex: 0x1434CB, opacity: 0.6), lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
    }
}

/// Visa blue is for payment moments only.
struct VisaMark: View {
    var body: some View {
        Text("VISA")
            .font(.system(size: 11, weight: .bold))
            .tracking(1)
            .foregroundStyle(.white)
            .frame(width: 44, height: 28)
            .background(.visaBlue, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            .accessibilityLabel("Visa")
    }
}

#Preview("05 · Cart") {
    let app = AppState()
    app.cart = MockData.cart
    app.tab = .cart
    return RootView().environment(app)
}
