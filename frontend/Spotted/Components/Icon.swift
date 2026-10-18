import SwiftUI

/// An icon from Assets.xcassets (exported from Figma) at its design size.
struct Icon: View {
    let name: String
    var size: CGFloat = 20

    var body: some View {
        Image(name)
            .resizable()
            .scaledToFit()
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

/// Product imagery: "asset:<name>" loads bundled mock art, anything else loads as a URL
/// (thumbnails and cutouts from the backend).
struct ProductImage: View {
    let source: String

    var body: some View {
        Group {
            if source.hasPrefix("asset:") {
                Image(String(source.dropFirst("asset:".count)))
                    .resizable()
                    .scaledToFit()
            } else {
                AsyncImage(url: URL(string: source)) { image in
                    image.resizable().scaledToFit()
                } placeholder: {
                    Color.clear
                }
            }
        }
        .accessibilityHidden(true)
    }
}

/// A rounded well with product art inside, used by cards and list rows.
struct ProductThumbnail: View {
    let source: String
    var size: CGFloat = 60
    var radius: CGFloat = 16
    var background: Color = .surfaceRaised

    var body: some View {
        ProductImage(source: source)
            .frame(width: size * 0.87, height: size * 0.74)
            .frame(width: size, height: size)
            .background(background, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
    }
}

extension String {
    /// "lounge chair" → "Lounge chair"
    var sentenceCased: String { prefix(1).uppercased() + dropFirst() }
}
