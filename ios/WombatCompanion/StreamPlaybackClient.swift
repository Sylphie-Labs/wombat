//
//  StreamPlaybackClient.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  WebSocket playback client for WireContract.Route.stream (§6).
//
//  THE SAMPLE RATE IS NEVER A LITERAL HERE. It is read exactly twice, both times through
//  WireContract: once at init, from a prior WireContract.Health.Response.audio.sampleRateHz
//  (obtained via HealthHandshakeClient — see the composition root that builds this class),
//  and again from each utterance's own utterance_start TEXT frame
//  (WireContract.Stream.UtteranceStartEvent.sampleRateHz). The second read re-confirms the
//  first for THIS utterance; nothing here ever falls back to a number typed into this file.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

/// The outcome of one playback attempt. `.partial` matches wombat's own played_any=True
/// bookkeeping (§6): a socket that closes between utterance_start and utterance_end played
/// SOMETHING and must never be reported as a plain success.
public enum PlaybackOutcome {
    case completed(utteranceId: String)
    case partial(utteranceId: String)
    case failed
}

/// Buffers a small run of PCM chunks before handing them onward, so playback does not
/// stutter on the first few frames of an utterance while the network is still bursty.
final class JitterBuffer {
    private var chunks: [Data] = []
    private let minimumBufferedChunks: Int
    private(set) var isPrimed = false

    init(minimumBufferedChunks: Int = 3) {
        self.minimumBufferedChunks = minimumBufferedChunks
    }

    func push(_ chunk: Data) -> [Data] {
        chunks.append(chunk)
        guard isPrimed else {
            guard chunks.count >= minimumBufferedChunks else { return [] }
            isPrimed = true
            return drainAll()
        }
        return drainAll()
    }

    func drainRemainder() -> [Data] {
        drainAll()
    }

    func reset() {
        chunks.removeAll()
        isPrimed = false
    }

    private func drainAll() -> [Data] {
        defer { chunks.removeAll() }
        return chunks
    }
}

public final class StreamPlaybackClient: NSObject {
    private let connection: WombatConnection
    private var webSocketTask: URLSessionWebSocketTask?
    private let jitterBuffer = JitterBuffer()

    /// Sourced ONLY from a prior GET-health call's audio.sampleRateHz via WireContract.
    /// Never assigned a numeric literal — see the file header.
    private var knownSampleRateHz: Int

    /// Set from THIS utterance's utterance_start frame — re-confirms knownSampleRateHz
    /// rather than trusting the cached health value blindly.
    private var confirmedSampleRateHz: Int?

    private var currentUtteranceId: String?
    private var sawUtteranceStart = false

    public var onPCMChunk: ((Data) -> Void)?
    public var onOutcome: ((PlaybackOutcome) -> Void)?

    /// `healthSampleRateHz` MUST come from a prior
    /// WireContract.Health.Response.audio.sampleRateHz (i.e. a successful
    /// HealthHandshakeClient.checkHealth()), never a constant.
    public init(connection: WombatConnection, healthSampleRateHz: Int) {
        self.connection = connection
        self.knownSampleRateHz = healthSampleRateHz
    }

    public func connect() {
        guard let url = connection.endpoint.webSocketURL(path: WireContract.Route.stream) else {
            onOutcome?(.failed)
            return
        }
        guard let token = connection.deviceToken else {
            onOutcome?(.failed)
            return
        }

        var request = URLRequest(url: url)
        request.setValue(token, forHTTPHeaderField: WireContract.deviceTokenHeader)
        request.setValue(WireContract.Stream.subprotocol, forHTTPHeaderField: "Sec-WebSocket-Protocol")

        let task = URLSession.shared.webSocketTask(with: request)
        webSocketTask = task
        task.resume()
        listen()
    }

    public func disconnect() {
        webSocketTask?.cancel(with: .normalClosure, reason: nil)
        webSocketTask = nil
    }

    /// The rate in effect for the current utterance: the re-confirmed per-utterance value
    /// when one exists, else the cached health value as a pre-utterance baseline.
    public var effectiveSampleRateHz: Int {
        confirmedSampleRateHz ?? knownSampleRateHz
    }

    private func listen() {
        webSocketTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                self.handleSocketClosed()
            case .success(let message):
                self.handle(message)
                self.listen()
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .data(let chunk):
            for drained in jitterBuffer.push(chunk) {
                onPCMChunk?(drained)
            }
        case .string(let text):
            handleTextFrame(text)
        @unknown default:
            break
        }
    }

    private func handleTextFrame(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }

        if let start = try? JSONDecoder().decode(WireContract.Stream.UtteranceStartEvent.self, from: data),
           start.event == WireContract.Stream.eventUtteranceStart {
            currentUtteranceId = start.utteranceId
            sawUtteranceStart = true
            // Re-confirmation, per the file header: THIS utterance's own frame is the
            // authority for its rate, not the cached health value.
            confirmedSampleRateHz = start.sampleRateHz
            jitterBuffer.reset()
            return
        }

        if let end = try? JSONDecoder().decode(WireContract.Stream.UtteranceEndEvent.self, from: data),
           end.event == WireContract.Stream.eventUtteranceEnd {
            for drained in jitterBuffer.drainRemainder() {
                onPCMChunk?(drained)
            }
            onOutcome?(.completed(utteranceId: end.utteranceId))
            sawUtteranceStart = false
            currentUtteranceId = nil
            confirmedSampleRateHz = nil
        }
    }

    private func handleSocketClosed() {
        // A socket that closes between utterance_start and utterance_end is a PARTIAL
        // reply, never a plain success — matches played_any=True on the wombat side (§6).
        if sawUtteranceStart, let utteranceId = currentUtteranceId {
            onOutcome?(.partial(utteranceId: utteranceId))
        } else {
            onOutcome?(.failed)
        }
        sawUtteranceStart = false
        currentUtteranceId = nil
        confirmedSampleRateHz = nil
    }
}
