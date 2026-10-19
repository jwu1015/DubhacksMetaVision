import LocalAuthentication
import SwiftUI

/// 06 · Checkout: the pay sheet. Claude never pays: this sheet plus Face ID (or passcode)
/// is the only way to reach /checkout.
struct CheckoutSheet: View {
    @Environment(AppState.self) private var app
    @State private var authError: String?

    var body: some View {
        VStack(spacing: 16) {
            Capsule()
                .fill(.white.opacity(0.3))
                .frame(width: 36, height: 5)

            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Confirm purchase")
                        .font(.spottedTitle2)
                        .foregroundStyle(.labelPrimary)
                    Text("Storefront agent checkout · secure handoff")
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Button { app.isShowingCheckout = false } label: {
                    Icon(name: "icon-close-small", size: 18)
                        .frame(width: 36, height: 36)
                        .background(.white.opacity(0.12), in: Circle())
                        .frame(width: 44, height: 44)
                        .contentShape(Circle())
                }
                .buttonStyle(PressableStyle())
                .accessibilityLabel("Close")
            }

            details

            HStack(spacing: 10) {
                Icon(name: "icon-glasses-accent", size: 20)
                Text("“\(Money.spoken(app.total)) to Visa \(app.agentLimit.cardLast4). Confirm on your phone.”")
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelPrimary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(Color.accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).strokeBorder(Color.accent.opacity(0.25), lineWidth: 1))
            .accessibilityElement(children: .combine)

            HStack {
                Text("Total")
                    .font(.spottedHeadline)
                Spacer()
                Text(Money.exact(app.total))
                    .font(.spottedPrice)
            }
            .foregroundStyle(.labelPrimary)
            .accessibilityElement(children: .combine)

            VStack(spacing: 10) {
                Icon(name: "icon-faceid", size: 52)
                Text(authError ?? (app.isWithinLimit ? "Confirm with Face ID to continue" : "Over your limit. Approve on your phone."))
                    .font(.spottedFootnote)
                    .foregroundStyle(authError == nil && app.isWithinLimit ? Color.labelSecondary : Color.error)
                    .multilineTextAlignment(.center)
                PrimaryButton(title: "Continue to secure checkout", height: 56) {
                    authenticate(.deviceOwnerAuthenticationWithBiometrics)
                }
                .disabled(!app.isWithinLimit)
                .opacity(app.isWithinLimit ? 1 : 0.4)
                HStack(spacing: 6) {
                    Icon(name: "icon-lock", size: 12)
                    Text("Payments by Stripe · Test mode")
                        .font(.spottedCaption)
                        .foregroundStyle(.labelTertiary)
                }
                Button("Use passcode instead") {
                    authenticate(.deviceOwnerAuthentication)
                }
                .font(.spottedFootnote)
                .foregroundStyle(.labelPrimary)
                .frame(minHeight: 36)
                .disabled(!app.isWithinLimit)
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 10)
        .padding(.bottom, 24)
        .glass(in: RoundedRectangle(cornerRadius: 40, style: .continuous), tint: 0.92)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
        .ignoresSafeArea(edges: .bottom)
    }

    private var details: some View {
        VStack(spacing: 0) {
            detailRow("Merchants") {
                Text(app.merchants)
            }
            detailRow("Items") {
                Text(app.cart.map(\.shortTitle).joined(separator: ", "))
            }
            detailRow("Pay with") {
                HStack(spacing: 8) {
                    MiniVisaCard()
                    Text("Visa ••\(app.agentLimit.cardLast4) · Test")
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Icon(name: "icon-chevron", size: 16)
                }
            }
            detailRow("Agent limit") {
                HStack(spacing: 8) {
                    if app.isWithinLimit {
                        Icon(name: "icon-check-success", size: 16)
                        Text("Within \(Money.short(app.agentLimit.amount)) limit")
                    } else {
                        Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.error)
                        Text("Over \(Money.short(app.agentLimit.amount)) limit")
                    }
                }
            }
        }
        .background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    private func detailRow<Value: View>(_ label: String, @ViewBuilder value: () -> Value) -> some View {
        HStack(spacing: 12) {
            Text(label)
                .font(.spottedFootnote)
                .foregroundStyle(.labelSecondary)
                .frame(width: 78, alignment: .leading)
            value()
                .font(.spottedSubhead)
                .foregroundStyle(.labelPrimary)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 13)
        .overlay(alignment: .top) {
            if label != "Merchants" {
                Rectangle().fill(.white.opacity(0.08)).frame(height: 1)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func authenticate(_ policy: LAPolicy) {
        authError = nil
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(policy, error: &error) else {
            authError = policy == .deviceOwnerAuthenticationWithBiometrics
                ? "Face ID isn't available. Use your passcode."
                : "Set a passcode on this phone to check out."
            return
        }
        Task {
            do {
                _ = try await context.evaluatePolicy(
                    policy,
                    localizedReason: "Confirm \(Money.exact(app.total)) to Visa ••\(app.agentLimit.cardLast4)"
                )
                // TODO(build step 9): POST /checkout with the cart, then open checkout_url
                // in SFSafariViewController (06b · Stripe Handoff).
                app.isShowingCheckout = false
            } catch {
                authError = "Not confirmed. Try again or use your passcode."
            }
        }
    }
}

private struct MiniVisaCard: View {
    var body: some View {
        Text("VISA")
            .font(.system(size: 9, weight: .bold))
            .tracking(0.8)
            .foregroundStyle(.white)
            .padding(.leading, 8)
            .padding(.bottom, 2)
            .frame(width: 44, height: 28, alignment: .bottomLeading)
            .background(
                LinearGradient(colors: [Color(hex: 0x1434CB), Color(hex: 0x0D1A73)], startPoint: .leading, endPoint: .trailing),
                in: RoundedRectangle(cornerRadius: 5, style: .continuous)
            )
            .accessibilityHidden(true)
    }
}

#Preview("06 · Checkout") {
    let app = AppState()
    app.cart = MockData.cart
    app.tab = .cart
    app.isShowingCheckout = true
    return RootView().environment(app)
}
