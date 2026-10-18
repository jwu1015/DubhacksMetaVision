import SwiftUI

/// Before anything streams: pick the camera (the glasses only when they're connected), see where the
/// app will connect, and start.
struct StartView: View {
    @Environment(AppState.self) private var app
    @State private var source: CaptureMode = .phone
    @State private var category = TrackingCategory.default

    var body: some View {
        VStack(spacing: 0) {
            Spacer()
            VStack(spacing: 10) {
                Icon(name: "icon-glasses-accent", size: 44)
                Text("Spotted")
                    .font(.spottedLargeTitle)
                    .foregroundStyle(.labelPrimary)
                Text("Point your camera at something to see what it is and what it costs.")
                    .font(.spottedSubhead)
                    .foregroundStyle(.labelSecondary)
                    .multilineTextAlignment(.center)
            }
            .padding(.horizontal, 32)
            Spacer()
            VStack(alignment: .leading, spacing: 10) {
                Text("LOOK FOR")
                    .font(.spottedCaption)
                    .tracking(0.72)
                    .foregroundStyle(.labelTertiary)
                FlowLayout(spacing: 8) {
                    ForEach(TrackingCategory.all) { option in
                        CategoryChip(title: option.title, isSelected: option == category) { category = option }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 20)
            .padding(.bottom, 20)
            VStack(spacing: 10) {
                SourceRow(title: "Phone camera", detail: "The iPhone's back camera", icon: "icon-camera",
                          isSelected: source == .phone, isEnabled: true) { source = .phone }
                SourceRow(title: "Meta glasses", detail: glassesDetail, icon: "icon-glasses",
                          isSelected: source == .glasses, isEnabled: hasGlasses) { source = .glasses }
                if let glasses = app.glasses, !glasses.isRegistered {
                    Button("Connect glasses in the Meta AI app", action: glasses.connect)
                        .font(.spottedFootnote)
                        .foregroundStyle(Color.accent)
                        .disabled(glasses.registration == .registering)
                }
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 24)
            VStack(spacing: 12) {
                PrimaryButton(title: "Start streaming", height: 56) { app.startStreaming(from: source, category: category.value) }
                Text(server)
                    .font(.spottedFootnote)
                    .foregroundStyle(.labelTertiary)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 24)
        }
        .frame(maxWidth: .infinity)
        .background(Color.ink)
        // The glasses went away while picked: back to the phone.
        .onChange(of: hasGlasses) { _, available in
            if !available { source = .phone }
        }
    }

    private var hasGlasses: Bool { app.glasses?.hasGlasses == true }

    private var glassesDetail: String {
        guard let glasses = app.glasses else { return "Not available on this device" }
        if hasGlasses { return "Connected" }
        if glasses.registration == .registering { return "Connecting in Meta AI…" }
        return glasses.isRegistered ? "No glasses found · turn them on" : "No glasses found"
    }

    private var server: String {
        AppConfig.baseURL?.host().map { "Connects to \($0)" } ?? "No backend set: demo data"
    }
}

/// One category to track. The selected one is lime.
private struct CategoryChip: View {
    let title: String
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.spottedCallout)
                .foregroundStyle(isSelected ? Color.onAccent : Color.labelPrimary)
                .padding(.horizontal, 14)
                .frame(height: 36)
                .background(isSelected ? Color.accent : Color.surface, in: Capsule())
        }
        .buttonStyle(PressableStyle())
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

/// One camera to pick from. Dimmed and inert when it isn't available.
private struct SourceRow: View {
    let title: String
    let detail: String
    let icon: String
    let isSelected: Bool
    let isEnabled: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Icon(name: icon, size: 22)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.spottedCallout)
                        .foregroundStyle(.labelPrimary)
                    Text(detail)
                        .font(.spottedFootnote)
                        .foregroundStyle(.labelSecondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Circle()
                    .strokeBorder(isSelected ? Color.accent : Color.white.opacity(0.3), lineWidth: 2)
                    .background(Circle().fill(isSelected ? Color.accent : .clear).padding(5))
                    .frame(width: 22, height: 22)
            }
            .padding(.horizontal, 16)
            .frame(minHeight: 64)
            .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .strokeBorder(isSelected ? Color.accent : .clear, lineWidth: 1.5)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(PressableStyle())
        .disabled(!isEnabled)
        .opacity(isEnabled ? 1 : 0.45)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

#Preview("Start") {
    StartView().environment(AppState())
}
