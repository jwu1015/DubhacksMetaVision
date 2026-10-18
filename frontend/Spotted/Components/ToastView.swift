import SwiftUI

/// "Added · Walnut Lounge Chair · Undo". Save and add are instant but undoable for 10 s.
struct ToastView: View {
    @Environment(AppState.self) private var app
    let toast: AppState.Toast

    var body: some View {
        HStack(spacing: 12) {
            Text(toast.text)
                .font(.spottedSubhead)
                .foregroundStyle(.labelPrimary)
                .lineLimit(1)
            if toast.canUndo {
                Button("Undo") {
                    Task { await app.undoLast() }
                }
                .font(.spottedHeadline)
                .foregroundStyle(.accent)
                .frame(minHeight: 44)
            }
        }
        .padding(.leading, 18)
        .padding(.trailing, toast.canUndo ? 14 : 18)
        .frame(minHeight: 48)
        .glass(in: Capsule(), tint: 0.78)
        .padding(.horizontal, 16)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
    }
}

extension View {
    /// Shows the app's current toast above the bottom edge.
    func toastOverlay(_ app: AppState, bottomPadding: CGFloat) -> some View {
        overlay(alignment: .bottom) {
            if let toast = app.toast {
                ToastView(toast: toast)
                    .padding(.bottom, bottomPadding)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                    .id(toast.id)
            }
        }
        .animation(.easeOut(duration: 0.2), value: app.toast)
    }
}
