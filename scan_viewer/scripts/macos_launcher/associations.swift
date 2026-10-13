import AppKit
import CoreServices
import UniformTypeIdentifiers

func snapshot(_ extensions: [String]) -> [[String: Any]] {
    extensions.map { ext in
        let identifier = UTType(filenameExtension: ext)!.identifier
        let handler = LSCopyDefaultRoleHandlerForContentType(identifier as CFString, .all)?.takeRetainedValue()
        return ["extension": ext, "type": identifier, "handler": handler as String? ?? ""]
    }
}

let args = Array(CommandLine.arguments.dropFirst())
do {
    switch args.first {
    case "snapshot":
        let data = try JSONSerialization.data(withJSONObject: snapshot(Array(args.dropFirst())), options: [.prettyPrinted, .sortedKeys])
        FileHandle.standardOutput.write(data)
    case "set":
        guard args.count >= 3 else { throw NSError(domain: "ScanViewer", code: 1) }
        let app = URL(fileURLWithPath: args[1])
        for ext in args.dropFirst(2) {
            let type = UTType(filenameExtension: ext)!
            var completed = false
            var failure: Error?
            NSWorkspace.shared.setDefaultApplication(at: app, toOpen: type) { error in
                failure = error
                completed = true
            }
            while !completed {
                RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
            }
            if let failure { throw failure }
            guard NSWorkspace.shared.urlForApplication(toOpen: type)?.standardizedFileURL == app.standardizedFileURL else {
                throw NSError(domain: "ScanViewer", code: 2, userInfo: [NSLocalizedDescriptionKey: "Could not set the default opener for .\(ext)."])
            }
        }
    case "handler":
        guard args.count == 2 else { throw NSError(domain: "ScanViewer", code: 1) }
        let file = URL(fileURLWithPath: args[1])
        print(NSWorkspace.shared.urlForApplication(toOpen: file)?.path ?? "")
    default:
        throw NSError(domain: "ScanViewer", code: 1, userInfo: [NSLocalizedDescriptionKey: "Use snapshot, set, or handler."])
    }
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
