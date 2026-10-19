import SwiftUI

/// iOS 26 floating Liquid Glass tab bar. Selected tab = accent icon + label on a light glass pill
/// (shape + color, not color alone).
struct GlassTabBar: View {
    @Binding var selection: AppState.Tab

    var body: some View {
        HStack(spacing: 0) {
            ForEach(AppState.Tab.allCases, id: \.self) { tab in
                let isSelected = tab == selection
                Button {
                    selection = tab
                } label: {
                    VStack(spacing: 3) {
                        icon(for: tab, selected: isSelected)
                        Text(tab.rawValue)
                            .font(.storefrontTabLabel)
                            .tracking(0.1)
                            .foregroundStyle(isSelected ? Color.accent : Color.labelPrimary)
                    }
                    .frame(width: 70, height: 54)
                    .background {
                        if isSelected { Capsule().fill(.white.opacity(0.14)) }
                    }
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(isSelected ? .isSelected : [])
            }
        }
        .padding(4)
        .glass(in: Capsule(), tint: 0.42)
    }

    @ViewBuilder
    private func icon(for tab: AppState.Tab, selected: Bool) -> some View {
        switch tab {
        case .live: Icon(name: selected ? "tab-live-selected" : "tab-live", size: 24)
        case .saved: Icon(name: selected ? "tab-saved-selected" : "tab-saved", size: 24)
        case .cart: Icon(name: selected ? "tab-cart-selected" : "tab-cart", size: 24)
        // Figma has no selected Profile icon yet, so tint the white one.
        case .profile:
            Image("tab-profile")
                .renderingMode(.template)
                .resizable()
                .frame(width: 24, height: 24)
                .foregroundStyle(selected ? Color.accent : Color.labelPrimary)
                .accessibilityHidden(true)
        }
    }
}

/// Push-to-talk. 62pt glass circle, 8pt from the tab bar.
struct MicButton: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Icon(name: "icon-mic", size: 26)
                .frame(width: 62, height: 62)
                .glass(in: Circle(), tint: 0.42)
        }
        .buttonStyle(PressableStyle())
        .accessibilityLabel("Talk to Storefront")
    }
}
