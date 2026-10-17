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
