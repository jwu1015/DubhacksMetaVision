import SwiftUI

/// Pill segmented control used on Product Detail (Best match / Dupes / Used) and Saved (Saved / History).
struct SegmentedControl<Option: Hashable>: View {
    let options: [Option]
    let title: (Option) -> String
    @Binding var selection: Option
    @Namespace private var namespace

    var body: some View {
        HStack(spacing: 0) {
            ForEach(options, id: \.self) { option in
                let isSelected = option == selection
                Button {
                    withAnimation(.easeOut(duration: 0.2)) { selection = option }
                } label: {
                    Text(title(option))
                        .font(.storefrontFootnote.weight(isSelected ? .semibold : .regular))
                        .foregroundStyle(.labelPrimary)
                        .frame(maxWidth: .infinity)
                        .frame(height: 34)
                        .background {
                            if isSelected {
                                Capsule()
                                    .fill(.white.opacity(0.18))
                                    .matchedGeometryEffect(id: "selection", in: namespace)
                            }
                        }
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(isSelected ? .isSelected : [])
            }
        }
        .padding(3)
        .background(.white.opacity(0.08), in: Capsule())
    }
}
