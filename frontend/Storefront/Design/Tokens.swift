import SwiftUI

/// Colors from the Figma "Spotted Color" variable collection.
/// Lime is the default; Visa Night is ready if we switch themes for the demo.
struct Palette {
    let ink: Color
    let surface: Color
    let surfaceRaised: Color
    let separator: Color
    let labelPrimary: Color
    let labelSecondary: Color
    let labelTertiary: Color
    let onAccent: Color
    let accent: Color
    let accentPressed: Color
    let coral: Color
    let visaBlue: Color
    let success: Color
    let warning: Color
    let error: Color

    static let lime = Palette(
        ink: Color(hex: 0x0A0A0C),
        surface: Color(hex: 0x16161A),
        surfaceRaised: Color(hex: 0x222228),
        separator: Color(hex: 0x2C2C32),
        labelPrimary: .white,
        labelSecondary: Color(hex: 0xA1A1AA),
        labelTertiary: Color(hex: 0x8A8A94),
        onAccent: Color(hex: 0x0A0A0C),
        accent: Color(hex: 0xC8FF3D),
        accentPressed: Color(hex: 0xA8DB1F),
        coral: Color(hex: 0xFF7A59),
        visaBlue: Color(hex: 0x1434CB),
        success: Color(hex: 0x30D158),
        warning: Color(hex: 0xFFD60A),
        error: Color(hex: 0xFF6961)
    )

    static let visaNight = Palette(
        ink: Color(hex: 0x070B24),
        surface: Color(hex: 0x0F1638),
        surfaceRaised: Color(hex: 0x18214D),
        separator: Color(hex: 0x222B57),
        labelPrimary: .white,
        labelSecondary: Color(hex: 0xAAB3D6),
        labelTertiary: Color(hex: 0x8892BD),
        onAccent: Color(hex: 0x0A0F2E),
        accent: Color(hex: 0xF7B600),
        accentPressed: Color(hex: 0xD99F00),
        coral: Color(hex: 0xFF8C7A),
        visaBlue: Color(hex: 0x1434CB),
        success: Color(hex: 0x4ADE9B),
        warning: Color(hex: 0xFF9F43),
        error: Color(hex: 0xFF7A85)
    )
}

enum Theme {
    /// The icon SVGs in Assets.xcassets are drawn in Lime colors, so switching
    /// to Visa Night also needs the accent-colored icons re-exported.
    static let palette: Palette = .lime
}

extension ShapeStyle where Self == Color {
    static var ink: Color { Theme.palette.ink }
    static var surface: Color { Theme.palette.surface }
    static var surfaceRaised: Color { Theme.palette.surfaceRaised }
    static var separator: Color { Theme.palette.separator }
    static var labelPrimary: Color { Theme.palette.labelPrimary }
    static var labelSecondary: Color { Theme.palette.labelSecondary }
    static var labelTertiary: Color { Theme.palette.labelTertiary }
    static var onAccent: Color { Theme.palette.onAccent }
    static var accent: Color { Theme.palette.accent }
    static var accentPressed: Color { Theme.palette.accentPressed }
    static var coral: Color { Theme.palette.coral }
    static var visaBlue: Color { Theme.palette.visaBlue }
    static var success: Color { Theme.palette.success }
    static var warning: Color { Theme.palette.warning }
    static var error: Color { Theme.palette.error }
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: opacity
        )
    }
}

/// Figma text styles mapped to SwiftUI system styles, so we get SF Pro and Dynamic Type.
extension Font {
    static let storefrontLargeTitle = Font.largeTitle.bold()
    static let storefrontTitle1 = Font.title.bold()
    static let storefrontTitle2 = Font.title2.bold()
    static let storefrontHeadline = Font.headline
    static let storefrontCallout = Font.callout.weight(.medium)
    static let storefrontSubhead = Font.subheadline
    static let storefrontFootnote = Font.footnote
    static let storefrontCaption = Font.caption.weight(.medium)
    static let storefrontTabLabel = Font.caption2.weight(.medium)
    static let storefrontPrice = Font.largeTitle.bold().monospacedDigit()
    static let storefrontTranscript = Font.title2.weight(.medium)
}

enum Money {
    private static let whole: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = "USD"
        f.locale = Locale(identifier: "en_US")
        f.maximumFractionDigits = 0
        return f
    }()

    private static let cents: NumberFormatter = {
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = "USD"
        f.locale = Locale(identifier: "en_US")
        f.minimumFractionDigits = 2
        f.maximumFractionDigits = 2
        return f
    }()

    /// "$1,299" for whole amounts, "$1,642.01" otherwise.
    static func short(_ value: Double) -> String {
        let formatter = value.rounded() == value ? whole : cents
        return formatter.string(from: NSNumber(value: value)) ?? ""
    }

    /// Always shows cents: "$1,488.00".
    static func exact(_ value: Double) -> String {
        cents.string(from: NSNumber(value: value)) ?? ""
    }

    /// Rounded to whole dollars for speech: "$1,642".
    static func spoken(_ value: Double) -> String {
        whole.string(from: NSNumber(value: value.rounded())) ?? ""
    }
}
