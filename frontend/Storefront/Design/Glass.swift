import SwiftUI

/// "Regular" Liquid Glass from the design system: glass tinted with bg/surface
/// plus a 1pt inside stroke (white at 18%). Use it for anything with text over the camera feed.
struct GlassModifier<S: InsettableShape>: ViewModifier {
    let shape: S
    let tint: Double

    func body(content: Content) -> some View {
        if #available(iOS 26, *) {
            content
                .glassEffect(.regular.tint(Color.surface.opacity(tint)), in: shape)
                .overlay(shape.strokeBorder(.white.opacity(0.18), lineWidth: 1))
        } else {
            content
                .background {
                    shape.fill(.ultraThinMaterial)
                    shape.fill(Color.surface.opacity(tint))
                }
                .overlay(shape.strokeBorder(.white.opacity(0.18), lineWidth: 1))
        }
    }
}

extension View {
    /// Tint is the bg/surface alpha from Figma (0.42 for the tab bar, 0.62 for cards, 0.92 for the pay sheet).
    func glass<S: InsettableShape>(in shape: S, tint: Double = 0.62) -> some View {
        modifier(GlassModifier(shape: shape, tint: tint))
    }

    /// The flat fill used for buttons and chips that sit on glass or surfaces.
    func fill<S: InsettableShape>(in shape: S, opacity: Double = 0.12, stroke: Double? = 0.16) -> some View {
        background(shape.fill(.white.opacity(opacity)))
            .overlay {
                if let stroke {
                    shape.strokeBorder(.white.opacity(stroke), lineWidth: 1)
                }
            }
    }
}
