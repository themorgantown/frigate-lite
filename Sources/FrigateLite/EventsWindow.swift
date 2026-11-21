import AppKit
import AVKit
import SwiftUI

/// Event list with thumbnails, and the saved clip (or snapshot) of the selected event.
/// Loads only on open, refresh or selection; nothing polls in the background.
@MainActor
final class EventsWindowController: NSWindowController, NSWindowDelegate {
    private let model: EventsModel

    /// `connection` returns the viewer's current client and config, so this window
    /// follows reconnects and Settings changes instead of keeping its own login.
    init(connection: @escaping () -> (FrigateClient?, FrigateConfig?)) {
        model = EventsModel(connection: connection)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 640),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: true)
        window.title = "Events"
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 700, height: 400)
        window.tabbingMode = .disallowed
        window.contentViewController = NSHostingController(rootView: EventsView(model: model))
        if !window.setFrameUsingName("FrigateLiteEvents") { window.center() }
        window.setFrameAutosaveName("FrigateLiteEvents")
        super.init(window: window)
        window.delegate = self
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func present(camera: String?) {
        model.camera = camera
        model.reload()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    func reloadIfOpen() {
        if window?.isVisible == true { model.reload() }
    }

    func windowWillClose(_ notification: Notification) { model.stop() }
}

@MainActor @Observable
final class EventsModel {
    var events: [FrigateEvent] = []
    var cameras: [Camera] = []
    /// Camera filter; nil = all cameras.
    var camera: String?
    var listStatus: String?
    var selection: FrigateEvent.ID?
    var player: AVPlayer?
    var still: NSImage?
    var detailStatus: String?

    @ObservationIgnored private let connection: () -> (FrigateClient?, FrigateConfig?)
    @ObservationIgnored private var thumbnails: [String: NSImage] = [:]
    @ObservationIgnored private var listTask: Task<Void, Never>?
    @ObservationIgnored private var detailTask: Task<Void, Never>?
    @ObservationIgnored private var clipFile: URL?
    private static let clipDir = FileManager.default.temporaryDirectory.appendingPathComponent("FrigateLiteClips")

    init(connection: @escaping () -> (FrigateClient?, FrigateConfig?)) {
        self.connection = connection
    }

    var selected: FrigateEvent? { events.first { $0.id == selection } }

    func title(ofCamera name: String) -> String {
        cameras.first { $0.name == name }?.title ?? FrigateConfig.prettify(name)
    }

    func reload() {
        listTask?.cancel()
        let (client, config) = connection()
        cameras = config?.cameras ?? []
        guard let client else {
            events = []
            listStatus = "Not connected to Frigate yet. Check Settings (⌘,)."
            return
        }
        listStatus = events.isEmpty ? "Loading events…" : nil
        let camera = camera
        listTask = Task {
            do {
                let list = try await client.events(camera: camera, limit: 100)
                guard !Task.isCancelled else { return }
                events = list
                listStatus = list.isEmpty ? "No events" : nil
                if selected == nil { selection = nil }
                // Dev only: `-snapshotEvents YES` opens the first event so the snapshot shows a detail.
                if selection == nil, UserDefaults.standard.bool(forKey: "snapshotEvents") { selection = list.first?.id }
            } catch {
                guard !Task.isCancelled else { return }
                events = []
                listStatus = "Can't load events.\n\n" + FrigateClient.describe(error)
            }
        }
    }

    func thumbnail(for event: FrigateEvent) async -> NSImage? {
        if let cached = thumbnails[event.id] { return cached }
        guard let client = connection().0,
              let data = try? await client.eventImage(id: event.id, file: "thumbnail.jpg"),
              let image = NSImage(data: data) else { return nil }
        thumbnails[event.id] = image
        return image
    }

    /// Shows the selected event: its clip if Frigate saved one, otherwise its snapshot.
    func showSelection() {
        stopDetail()
        guard let event = selected, let client = connection().0 else { return }
        still = thumbnails[event.id]
        detailTask = Task {
            if event.hasClip {
                detailStatus = "Loading clip…"
                let dest = Self.clipDir.appendingPathComponent(UUID().uuidString + ".mp4")
                do {
                    try FileManager.default.createDirectory(at: Self.clipDir, withIntermediateDirectories: true)
                    try await client.downloadClip(id: event.id, to: dest)
                    guard !Task.isCancelled else { try? FileManager.default.removeItem(at: dest); return }
                    clipFile = dest
                    let player = AVPlayer(url: dest)
                    self.player = player
                    detailStatus = nil
                    player.play()
                } catch {
                    try? FileManager.default.removeItem(at: dest)
                    guard !Task.isCancelled else { return }
                    detailStatus = "Can't load the clip.\n" + FrigateClient.describe(error)
                }
            } else if event.hasSnapshot {
                if let data = try? await client.eventImage(id: event.id, file: "snapshot.jpg"),
                   let image = NSImage(data: data), !Task.isCancelled {
                    still = image
                }
                if !Task.isCancelled { detailStatus = "No clip saved for this event" }
            } else {
                detailStatus = "No clip or snapshot saved for this event"
            }
        }
    }

    func stop() {
        listTask?.cancel()
        stopDetail()
        // Clips can be large; leave nothing behind from this or an earlier run.
        try? FileManager.default.removeItem(at: Self.clipDir)
    }

    private func stopDetail() {
        detailTask?.cancel()
        player?.pause()
        player = nil
        still = nil
        detailStatus = nil
        if let clipFile { try? FileManager.default.removeItem(at: clipFile) }
        clipFile = nil
    }
}

private struct EventsView: View {
    @Bindable var model: EventsModel

    var body: some View {
        HSplitView {
            VStack(spacing: 0) {
                HStack {
                    Picker("Camera", selection: $model.camera) {
                        Text("All Cameras").tag(String?.none)
                        ForEach(model.cameras, id: \.name) { Text($0.title).tag(Optional($0.name)) }
                    }
                    .labelsHidden()
                    Button { model.reload() } label: { Image(systemName: "arrow.clockwise") }
                        .help("Refresh")
                }
                .padding(8)
                List(model.events, selection: $model.selection) { event in
                    EventRow(event: event, model: model)
                }
                .overlay {
                    if let status = model.listStatus {
                        Text(status).foregroundStyle(.secondary).multilineTextAlignment(.center).padding()
                    }
                }
            }
            .frame(minWidth: 280, idealWidth: 340, maxWidth: 500)

            EventDetail(model: model)
                .frame(minWidth: 400, maxWidth: .infinity, maxHeight: .infinity)
        }
        .onChange(of: model.camera) { model.reload() }
        .onChange(of: model.selection) { model.showSelection() }
    }
}

private struct EventRow: View {
    let event: FrigateEvent
    let model: EventsModel
    @State private var thumbnail: NSImage?

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                Color.black
                if let thumbnail { Image(nsImage: thumbnail).resizable().scaledToFill() }
            }
            .frame(width: 72, height: 48)
            .clipShape(RoundedRectangle(cornerRadius: 4))

            VStack(alignment: .leading, spacing: 2) {
                Text(event.heading).bold()
                Text(([model.title(ofCamera: event.camera)] + event.zones.map(FrigateConfig.prettify))
                        .joined(separator: " · "))
                    .foregroundStyle(.secondary)
                Text(event.timing).font(.caption).foregroundStyle(.secondary)
            }
            .lineLimit(1)
            Spacer(minLength: 0)
            if !event.hasClip {
                Image(systemName: "video.slash").foregroundStyle(.tertiary).help("No clip saved")
            }
        }
        .padding(.vertical, 2)
        .task(id: event.id) { thumbnail = await model.thumbnail(for: event) }
    }
}

private struct EventDetail: View {
    let model: EventsModel

    var body: some View {
        if let event = model.selected {
            VStack(alignment: .leading, spacing: 8) {
                ZStack {
                    Color.black
                    if let player = model.player {
                        PlayerView(player: player)
                    } else if let still = model.still {
                        Image(nsImage: still).resizable().scaledToFit()
                    }
                    if let status = model.detailStatus {
                        Text(status)
                            .multilineTextAlignment(.center)
                            .padding(8)
                            .background(.black.opacity(0.6), in: RoundedRectangle(cornerRadius: 6))
                            .foregroundStyle(.white)
                    }
                }
                Text(event.heading).font(.title2).bold()
                Text(model.title(ofCamera: event.camera) + " · " + event.timing
                     + (event.score.map { " · \(Int(($0 * 100).rounded())) %" } ?? ""))
                    .foregroundStyle(.secondary)
            }
            .padding()
        } else {
            Text("Select an event").foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// AppKit's player. SwiftUI's `VideoPlayer` crashes at runtime in a SwiftPM-built app
/// ("failed to demangle superclass of VideoPlayerView").
private struct PlayerView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player { view.player = player }
    }
}

private extension FrigateEvent {
    var heading: String {
        FrigateConfig.prettify(label) + (subLabel.map { ": \($0)" } ?? "")
    }

    var timing: String {
        let when = start.formatted(.dateTime.month(.abbreviated).day().hour().minute())
        guard let end else { return when + " · in progress" }
        let seconds = Int(end.timeIntervalSince(start).rounded())
        return when + " · " + (seconds < 60 ? "\(seconds) s" : "\(seconds / 60) min \(seconds % 60) s")
    }
}
