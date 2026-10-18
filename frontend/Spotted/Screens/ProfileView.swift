import SwiftUI

/// Profile isn't designed yet (onboarding, card linking and limits are open in FRONTEND.md).
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
                        Toggle(isOn: Binding(get: { app.captureMode == .glasses },
                                             set: { app.setCaptureMode($0 ? .glasses : .phone) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Stream from the glasses")
                                    .font(.spottedCallout)
                                    .foregroundStyle(.labelPrimary)
                                Text(app.glasses?.hasGlasses == true
                                     ? "Instead of the phone camera"
                                     : "Connect your glasses first")
                                    .font(.spottedFootnote)
                                    .foregroundStyle(.labelSecondary)
                            }
                        }
                        .disabled(app.glasses?.hasGlasses != true && app.captureMode != .glasses)
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
        case .registered: glasses.hasGlasses ? "Glasses connected" : "Glasses registered"
        case .registering: "Connecting in Meta AI…"
        default: "Meta glasses"
        }
    }

    private var subtitle: String {
        switch glasses.camera {
        case .streaming: return "Camera streaming to Spotted"
        case .connecting: return "Starting the glasses camera…"
        case .paused: return "Paused · tap the side of your glasses to resume"
        case let .failed(message): return message
        case .idle:
            if glasses.registration == .unavailable { return "Needs the Meta AI app" }
            if !glasses.isRegistered { return "Connect through the Meta AI app" }
            return glasses.hasGlasses ? "Ready" : "Turn on your glasses to connect"
        }
    }
}
