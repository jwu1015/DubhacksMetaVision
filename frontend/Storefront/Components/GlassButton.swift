import SwiftUI

/// 44pt glass circle for top-of-screen controls (pause, back, share, more).
struct GlassIconButton: View {
    let icon: String
    var iconSize: CGFloat = 20
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Icon(name: icon, size: iconSize)
                .frame(width: 44, height: 44)
                .glass(in: Circle(), tint: 0.55)
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel(label)
    }
}

/// Icon-only button on a card: white 12% fill + 16% stroke.
struct CircleButton: View {
    let icon: String
    var size: CGFloat = 48
    var iconSize: CGFloat = 20
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Icon(name: icon, size: iconSize)
                .frame(width: size, height: size)
                .fill(in: Circle())
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel(label)
    }
}

/// Tinted accent: the primary action. Solid accent fill with on-accent text.
struct PrimaryButton: View {
    let title: String
    var icon: String?
    var iconSize: CGFloat = 20
    var height: CGFloat = 48
    var horizontalPadding: CGFloat = 16
    var expands = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ButtonLabel(title: title, icon: icon, iconSize: iconSize)
                .foregroundStyle(.onAccent)
                .padding(.horizontal, horizontalPadding)
                .frame(maxWidth: expands ? .infinity : nil)
                .frame(height: height)
        }
        .buttonStyle(PrimaryStyle())
    }
}

/// Secondary action next to a primary one.
struct SecondaryButton: View {
    let title: String
    var icon: String?
    var iconSize: CGFloat = 20
    var height: CGFloat = 48
    var horizontalPadding: CGFloat = 16
    var expands = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ButtonLabel(title: title, icon: icon, iconSize: iconSize)
                .foregroundStyle(.labelPrimary)
                .padding(.horizontal, horizontalPadding)
                .frame(maxWidth: expands ? .infinity : nil)
                .frame(height: height)
                .fill(in: Capsule())
        }
        .buttonStyle(PressableStyle())
    }
}

private struct ButtonLabel: View {
    let title: String
    let icon: String?
    let iconSize: CGFloat

    var body: some View {
        HStack(spacing: 6) {
            if let icon { Icon(name: icon, size: iconSize) }
            Text(title)
                .font(.storefrontHeadline)
                .lineLimit(1)
        }
    }
}

struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.75 : 1)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

private struct PrimaryStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed ? Color.accentPressed : Color.accent, in: Capsule())
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}
