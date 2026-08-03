//
//  WatchUtterancePlaybackController.swift
//  ios/WombatCompanion Watch App
//
//  TK-360 — DRAFT SOURCE (DEC-82 tier A, DEC-83).
//
//  THE TRIGGER: `turnWasSent()` is the ONLY entry point into this file, and it exists to be
//  called from the moment a turn this watch sent has actually gone out — the intended call
//  site is WatchTalkSessionController's successful primary-upload branch (see that file's
//  `attemptSend`), the same way WatchHealthClient's caller is documented there rather than
//  wired here. Nothing in this file starts a fetch on a timer, at launch, or speculatively;
//  every fetch loop below traces back to one `turnWasSent()` call. That is what keeps
//  playback reachable only from a fetch THIS watch itself initiated — there is no inbound
//  callback anywhere in this tree by which wombat could cause audio on the wrist.
//
//  THE PRIVACY PROPERTY, stated as what is actually enforceable (restated at DEC-83(g) from
//  an earlier, unenforceable wording): wombat has NO push path to any device, and wombat
//  only ever seals a reply to a remote-originated turn — never a brief, a draft, a
//  reflection or any proactive surfacing. That is NOT the same claim as "only a turn this
//  watch originated" — DEC-79(c) permits a phone-originated turn to fall through to the
//  watch buffer when no phone session is open — so every delivered payload is compared
//  against this watch's own device_id (from §4) and named accordingly. See `deliver`.
//
//  THE TWO REAL COSTS, expressed as UI states rather than left for a later pass: charging
//  blocks playback outright (`.chargingBlocked`, checked via WatchChargingMonitor
//  immediately before any audio would start), and `batteryCostNotice` is shown next to every
//  actual playback so the operator sees the cost, not just the feature.
//
//  Buffer-then-play only (TK-360 non_goals) — `fetchUtterance()` already returns a
//  fully-buffered `Data`, and playback below schedules ONE complete PCM buffer, never a
//  streamed or sentence-chunked sequence.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import AVFoundation
import Foundation

@MainActor
public final class WatchUtterancePlaybackController: ObservableObject {

    public enum PlaybackState: Equatable {
        case idle
        case waitingForReply
        /// Transient — the fetch loop keeps retrying while in this state.
        case unreachableRetrying
        /// Terminal for this turn — this watch's own token was revoked. No further fetch is
        /// attempted until the next `turnWasSent()` call.
        case revoked
        /// The genuine dead state: a reply was fetched and buffered successfully, but this
        /// watch is on its charger right now, so it is not played.
        case chargingBlocked(sourceDescription: String)
        case playing(sourceDescription: String)
    }

    /// TK-360 intent, verbatim cost: "roughly ten minutes of watch-speaker playback costs
    /// about an hour of watch battery" — shown wherever the operator will actually see
    /// playback happening, never left as a fact only recorded in a comment.
    public static let batteryCostNotice =
        "About ten minutes of watch-speaker playback costs roughly an hour of watch battery."

    private static let pollIntervalNanoseconds: UInt64 = 2_000_000_000

    @Published public private(set) var state: PlaybackState = .idle

    private let playbackClient: WatchUtterancePlaybackClient
    private let healthClient: WatchHealthClient
    private let chargingMonitor: WatchChargingMonitor

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()

    public init(
        playbackClient: WatchUtterancePlaybackClient,
        healthClient: WatchHealthClient,
        chargingMonitor: WatchChargingMonitor
    ) {
        self.playbackClient = playbackClient
        self.healthClient = healthClient
        self.chargingMonitor = chargingMonitor
    }

    /// Call this once a turn has actually gone out — never speculatively, never on a timer.
    public func turnWasSent() {
        Task { await runFetchLoop() }
    }

    private func runFetchLoop() async {
        state = .waitingForReply

        // §4 is read fresh here for two things this loop needs and holds no copy of its
        // own: this watch's own device_id (to tell an own-turn reply from a DEC-79(c)
        // cross-device one) and utterance_ttl_seconds (the §5 retry window).
        switch await healthClient.checkHealth() {
        case .unauthorized:
            state = .revoked
        case .unreachable:
            state = .unreachableRetrying
        case .ok(_, let healthValue):
            guard let healthValue else {
                state = .unreachableRetrying
                return
            }
            await pollForUtterance(
                ownDeviceId: healthValue.deviceId,
                ttlSeconds: healthValue.utteranceTtlSeconds
            )
        }
    }

    private func pollForUtterance(ownDeviceId: String, ttlSeconds: Int) async {
        let deadline = Date().addingTimeInterval(Double(ttlSeconds))

        while Date() < deadline {
            switch await playbackClient.fetchUtterance() {
            case .unauthorized:
                state = .revoked
                return
            case .unreachable:
                state = .unreachableRetrying
                try? await Task.sleep(nanoseconds: Self.pollIntervalNanoseconds)
            case .ok(let status, let payload):
                if status == WireContract.Utterance.deliveredStatus, let payload {
                    await deliver(payload, ownDeviceId: ownDeviceId)
                    return
                }
                // §5 `204` — the ordinary nothing-sealed-yet answer. Keep waiting within
                // the TTL rather than treating it as a failure of any kind.
                state = .waitingForReply
                try? await Task.sleep(nanoseconds: Self.pollIntervalNanoseconds)
            }
        }

        // The TTL elapsed with nothing delivered. §5 defines an unfetched utterance as
        // simply expiring — not a failure state — so this settles back to idle rather than
        // rendering an error for something that never arrived.
        state = .idle
    }

    private func deliver(_ payload: WatchUtterancePlaybackClient.Payload, ownDeviceId: String) async {
        // DEC-83(g)/DEC-79(c): a delivered reply may be an answer to a turn a phone sent,
        // relayed here because no phone session was open. Compare, never assume.
        let isCrossDevice = payload.headers.originDeviceId != ownDeviceId
        let sourceDescription = isCrossDevice
            ? "Playing a reply to a turn from another paired device"
            : "Playing wombat's reply"

        guard !chargingMonitor.isCharging else {
            state = .chargingBlocked(sourceDescription: sourceDescription)
            return
        }

        guard let buffer = Self.makeBuffer(from: payload) else {
            state = .idle
            return
        }

        state = .playing(sourceDescription: sourceDescription)
        play(buffer: buffer)
    }

    /// Builds ONE complete PCM buffer from the already-fully-buffered §5 body — never a
    /// partial or streamed buffer. The format comes from THIS payload's own headers, not
    /// from any cached copy, because a §5 delivery describes only the utterance it carries.
    private static func makeBuffer(from payload: WatchUtterancePlaybackClient.Payload) -> AVAudioPCMBuffer? {
        guard payload.headers.audioFormat == "pcm_s16le" else { return nil }
        guard
            let format = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: Double(payload.headers.sampleRateHz),
                channels: AVAudioChannelCount(payload.headers.channels),
                interleaved: true
            )
        else {
            return nil
        }

        let bytesPerFrame = 2 * payload.headers.channels
        guard bytesPerFrame > 0 else { return nil }
        let frameCount = AVAudioFrameCount(payload.audioData.count / bytesPerFrame)
        guard
            frameCount > 0,
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frameCount)
        else {
            return nil
        }
        buffer.frameLength = frameCount

        payload.audioData.withUnsafeBytes { rawBuffer in
            guard
                let base = rawBuffer.baseAddress,
                let destination = buffer.int16ChannelData?[0]
            else {
                return
            }
            let source = base.assumingMemoryBound(to: Int16.self)
            destination.update(from: source, count: Int(frameCount) * payload.headers.channels)
        }
        return buffer
    }

    private func play(buffer: AVAudioPCMBuffer) {
        let audioSession = AVAudioSession.sharedInstance()
        try? audioSession.setCategory(.playback, mode: .default)
        try? audioSession.setActive(true)

        if engine.attachedNodes.isEmpty {
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: buffer.format)
        }

        guard (try? engine.start()) != nil else {
            state = .idle
            return
        }

        player.scheduleBuffer(buffer, completionHandler: nil)
        player.play()
    }
}
