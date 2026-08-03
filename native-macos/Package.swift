// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "OpenCullNative",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "OpenCullNative", targets: ["OpenCullNative"]),
    ],
    targets: [
        .executableTarget(
            name: "OpenCullNative",
            path: "Sources/OpenCullNative"
        ),
    ]
)
