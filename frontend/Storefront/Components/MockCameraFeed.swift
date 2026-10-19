import SwiftUI

/// The Figma "Camera Feed/Living Room" scene, drawn on the 393×852 design canvas and stretched to
/// fill. Stands in for the glasses' video until GlassesManager is wired up, and keeps the mock
/// detection boxes lined up with the furniture.
struct MockCameraFeed: View {
    var showsLamp = true

    var body: some View {
        GeometryReader { geo in
            canvas
                .frame(width: 393, height: 852, alignment: .topLeading)
                .scaleEffect(x: geo.size.width / 393, y: geo.size.height / 852, anchor: .topLeading)
        }
        .clipped()
        .accessibilityHidden(true)
    }

    private var canvas: some View {
        ZStack(alignment: .topLeading) {
            LinearGradient(colors: [Color(hex: 0x4A433B), Color(hex: 0x2B2620)], startPoint: .top, endPoint: .bottom)

            // Window
            LinearGradient(colors: [Color(hex: 0xE8F1F6), Color(hex: 0x9DB8C8)], startPoint: .top, endPoint: .bottom)
                .clipShape(RoundedRectangle(cornerRadius: 4))
                .place(222, 64, 140, 230)
            Color(hex: 0x3A332C).place(290, 64, 4, 230)
            Color(hex: 0x3A332C).place(222, 176, 140, 4)
            // The SVG's Gaussian blur isn't rendered by asset catalogs, so blur it here.
            Image("feed-window-glow").resizable().blur(radius: 40).place(70, -60, 480, 580)

            // Art on the wall
            Color(hex: 0x1E1B18).clipShape(RoundedRectangle(cornerRadius: 2)).place(40, 110, 120, 90)
            LinearGradient(colors: [Color(hex: 0xC77C4A), Color(hex: 0x6B8A9C)], startPoint: .leading, endPoint: .trailing)
                .place(48, 118, 104, 74)

            // Floor
            LinearGradient(colors: [Color(hex: 0x5A4130), Color(hex: 0x2A1E15)], startPoint: .top, endPoint: .bottom)
                .place(0, 470, 393, 382)
            Color(hex: 0x3C352E).place(0, 462, 393, 10)
            Image("feed-rug").resizable().place(6, 500, 380, 120)
            Image("feed-plant").resizable().place(14, 262, 90, 220)
            Image("feed-chair-shadow").resizable().blur(radius: 7).place(82, 460, 254, 58)

            Image("art-lounge-chair").resizable().place(96, 300, 220, 187)
            if showsLamp {
                Image("art-arc-lamp").resizable().place(268, 326, 170, 144.5)
            }

            // Vignette and scrims that keep glass text legible
            EllipticalGradient(stops: [.init(color: .clear, location: 0.55), .init(color: .black.opacity(0.55), location: 1)],
                               center: .center, startRadiusFraction: 0, endRadiusFraction: 0.5)
                .place(0, 0, 393, 852)
            LinearGradient(colors: [.black.opacity(0.55), .clear], startPoint: .top, endPoint: .bottom)
                .place(0, 0, 393, 140)
            LinearGradient(colors: [.clear, .black.opacity(0.7)], startPoint: .top, endPoint: .bottom)
                .place(0, 560, 393, 292)
        }
    }
}

private extension View {
    /// Absolute placement on the design canvas, matching the Figma frame.
    func place(_ x: CGFloat, _ y: CGFloat, _ width: CGFloat, _ height: CGFloat) -> some View {
        frame(width: width, height: height).offset(x: x, y: y)
    }
}
