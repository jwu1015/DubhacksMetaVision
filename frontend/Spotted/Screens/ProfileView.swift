import SwiftUI

/// Profile isn't designed yet (onboarding, card linking and limits are open in FRONTEND.md).
/// For now it lists every Figma frame so the team can jump to each state in a demo or review.
struct ProfileView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Profile")
                        .font(.spottedLargeTitle)
                        .foregroundStyle(.labelPrimary)
                    Text("Not designed yet")
                        .font(.spottedSubhead)
                        .foregroundStyle(.labelSecondary)
                }

                VStack(alignment: .leading, spacing: 10) {
                    Text("GLASSES")
                        .font(.spottedCaption)
                        .tracking(0.72)
                        .foregroundStyle(.labelTertiary)
                    VStack(spacing: 0) {
                        if let glasses = app.glasses {
                            GlassesConnectionRow(glasses: glasses)
                        }
                        // Glasses mode without hardware: the phone camera stands in for the glasses.
                        Toggle(isOn: Binding(get: { app.captureMode == .glasses },
                                             set: { app.setCaptureMode($0 ? .glasses : .phone) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Glasses mode")
                                    .font(.spottedCallout)
                                    .foregroundStyle(.labelPrimary)
                                Text(app.glasses?.hasGlasses == true
                                     ? "On automatically while your glasses are connected"
                                     : "No preview, ask by voice · uses the phone camera")
                                    .font(.spottedFootnote)
                                    .foregroundStyle(.labelSecondary)
                            }
                        }
                        .tint(.accent)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 12)
                        .overlay(alignment: .top) {
                            if app.glasses != nil {
                                Rectangle().fill(.white.opacity(0.08)).frame(height: 1)
                            }
                        }
                    }
                    .background(.surface, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                }

                VStack(alignment: .leading, spacing: 10) {
                    Text("MOCKUP STATES")
                        .font(.spottedCaption)
                        .tracking(0.72)
                        .foregroundStyle(.labelTertiary)
                    VStack(spacing: 0) {
                        ForEach(Array(MockFrame.allCases.enumerated()), id: \.element.id) { index, frame in
                            Button { app.show(frame) } label: {
                                HStack {
                                    Text(frame.rawValue)
                                        .font(.spottedCallout)
                                        .foregroundStyle(.labelPrimary)
                                    Spacer()
                                    Icon(name: "icon-chevron", size: 16)
                                }
                                .padding(.horizontal, 16)
                                .frame(minHeight: 48)
                                .contentShape(Rectangle())
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
            }
            .padding(.horizontal, 20)
            .padding(.top, 42)
            .padding(.bottom, 110)
        }
        .scrollIndicators(.hidden)
        .overlay(alignment: .bottom) { ScrollEdgeFade() }
        .background(Color.ink)
    }
}

/// Meta glasses: registration with Meta AI, the camera stream's state, and Connect / Disconnect.
private struct GlassesConnectionRow: View {
    let glasses: GlassesManager

    var body: some View {
        HStack(spacing: 12) {
            Icon(name: glasses.hasGlasses ? "dot-live" : "dot-warning", size: 8)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.spottedCallout)
                    .foregroundStyle(.labelPrimary)
                Text(glasses.errorMessage ?? subtitle)
                    .font(.spottedFootnote)
                    .foregroundStyle(glasses.errorMessage == nil ? Color.labelSecondary : Color.error)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button(glasses.isRegistered ? "Disconnect" : "Connect") {
                glasses.isRegistered ? glasses.disconnect() : glasses.connect()
            }
            .font(.spottedCallout)
            .foregroundStyle(glasses.isRegistered ? Color.labelPrimary : Color.onAccent)
            .padding(.horizontal, 16)
            .frame(height: 40)
            .background(glasses.isRegistered ? Color.white.opacity(0.12) : Color.accent, in: Capsule())
            .buttonStyle(PressableStyle())
            .disabled(glasses.registration == .registering)
        }
        .padding(.leading, 16)
        .padding(.trailing, 12)
        .padding(.vertical, 12)
        .accessibilityElement(children: .contain)
    }

    private var title: String {
        switch glasses.registration {
        case .registered: glasses.devices.isEmpty ? "Glasses registered" : "Glasses connected"
        case .registering: "Connecting in Meta AI…"
        default: "Meta glasses"
        }
    }

    private var subtitle: String {
        switch glasses.camera {
        case .streaming: return "Camera streaming to Spotted"
        case .connecting: return "Starting the glasses camera…"
        case let .failed(message): return message
        case .idle:
            if glasses.registration == .unavailable { return "Needs the Meta AI app" }
            if !glasses.isRegistered { return "Connect through the Meta AI app" }
            return glasses.devices.isEmpty ? "Turn on your glasses to connect" : "Ready"
        }
    }
}
