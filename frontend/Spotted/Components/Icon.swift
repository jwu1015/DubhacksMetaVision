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
    @State private var loaded: UIImage?

    var body: some View {
        Group {
            if source.hasPrefix("asset:") {
                Image(String(source.dropFirst("asset:".count)))
                    .resizable()
                    .scaledToFit()
            } else if let loaded {
                Image(uiImage: loaded)
                    .resizable()
                    .scaledToFit()
            } else {
                Color.clear
            }
        }
        .accessibilityHidden(true)
        // Not AsyncImage: crops come from the backend, behind Cloudflare Access, so they need its token.
        .task(id: source) { await load() }
    }

    private func load() async {
        loaded = nil
        guard !source.hasPrefix("asset:"), let url = URL(string: source),
              let (data, _) = try? await URLSession.shared.data(for: AppConfig.request(url)) else { return }
        loaded = UIImage(data: data)
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
