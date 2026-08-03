//
//  WireContract.swift
//  ios/Shared
//
//  TK-355 — DRAFT SOURCE FOUNDATION (DEC-82 tier A, DEC-83).
//
//  THIS FILE MIRRORS planning/design/wire-contract.md. IT NEVER AUTHORS THE WIRE.
//  If a field name, unit suffix, enum member, status code, size cap or QR shape is not in
//  that document, it does not belong here — propose a spec amendment to the contract
//  instead of adding it in Swift.
//
//  Every URL, request header name, Codable payload struct and enum for the wire lives in
//  this ONE file (DEC-83(a)). No other file in this tree may declare a second home for a
//  route path, a header name or a payload shape. This is what makes a wombat-side change
//  a one-file edit instead of a silent mismatch (DEC-82(e)).
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

// MARK: - §0 Conventions

public enum WireContract {

    /// §0: JSON envelope — every request/response body carries "v":1 as its first key.
    public static let wireVersion = 1

    /// §0: Auth header. Inherits the shipped X-Wombat-Chat-Token / X-Wombat-Token
    /// convention — never `Authorization: Bearer`.
    public static let deviceTokenHeader = "X-Wombat-Device-Token"

    /// §0: A version bump is a new prefix, never a silent reshape.
    public static let pathPrefix = "/v1/"

    // MARK: §1 Route table (the closed set — DEC-78(c)). Exactly five. No sixth.

    public enum Route {
        public static let health = "/v1/health"          // GET  — TK-339
        public static let voice = "/v1/voice"             // POST — TK-340
        public static let biometrics = "/v1/biometrics"   // POST — TK-341
        public static let utterance = "/v1/utterance"      // GET  — TK-343
        public static let stream = "/v1/stream"            // GET (Upgrade) — TK-345
    }

    /// Builds request URLs against the paired host/port from the §8 QR payload.
    /// The literal "http" scheme lives HERE ONLY — §0 fixes plaintext HTTP/1.1, no TLS.
    public struct Endpoint {
        public let host: String
        public let port: Int

        public init(host: String, port: Int) {
            self.host = host
            self.port = port
        }

        public func url(path: String) -> URL? {
            var components = URLComponents()
            components.scheme = "http"
            components.host = host
            components.port = port
            components.path = path
            return components.url
        }

        /// §6: the phone opens the socket; wombat never dials.
        public func webSocketURL(path: String) -> URL? {
            var components = URLComponents()
            components.scheme = "ws"
            components.host = host
            components.port = port
            components.path = path
            return components.url
        }
    }

    // MARK: - §0.1 The client-side result trichotomy (required)

    /// Every device-side call site MUST distinguish these THREE outcomes, never two.
    /// Collapsing `.unauthorized` into `.unreachable` is the defect this type exists to
    /// prevent: a revoked device must say "re-pair me", not spin forever behind a
    /// "wombat is not here right now" spinner.
    public enum Result<Value> {
        /// DNS/connect/timeout failure. wombat is off, asleep, or off-LAN. Retryable,
        /// transient.
        case unreachable
        /// HTTP 401. The token was revoked or wombat's keyring was re-minted.
        /// NOT retryable — the device must surface "re-pair this device".
        case unauthorized
        /// A real answer: 2xx or any other 4xx/5xx that is not an auth failure, carrying
        /// the HTTP status and the decoded body (when one exists / decodes).
        case ok(status: Int, value: Value?)
    }

    /// §0: `401` body is byte-identical to `chat/surface.py` on every path, including
    /// unknown ones (DEC-78(b) anti-enumeration). Note this body has NO "v" key — it is
    /// the one documented exception to the envelope rule in §0.
    public struct UnauthorizedBody: Codable {
        public let error: String
    }

    // MARK: - §2 POST /v1/voice — audio ingest

    public enum Voice {
        /// §2: not multipart — the stdlib transport should not grow a multipart parser.
        /// The audio is the raw body; metadata rides headers.
        public static let contentType = "audio/wav"
        public static let capturedAtHeader = "X-Wombat-Captured-At"

        /// §2: body size cap.
        public static let maxBodyBytes = 10 * 1024 * 1024 // 10 MiB

        /// §2 response `202`. `utterance_id` is minted server-side at accept; the phone
        /// mints NO idempotency field (§3.3 — idempotency is derived server-side from the
        /// audio bytes' sha256).
        public struct AcceptedResponse: Codable {
            public let v: Int
            public let accepted: Bool
            public let utteranceId: String
            public let deviceId: String

            enum CodingKeys: String, CodingKey {
                case v, accepted
                case utteranceId = "utterance_id"
                case deviceId = "device_id"
            }
        }

        /// §2 response `409` — stale audio. Nothing is written to the drop dir.
        public struct StaleAudioResponse: Codable {
            public let v: Int
            public let error: String
            public let staleAudioWindowSeconds: Int

            enum CodingKeys: String, CodingKey {
                case v, error
                case staleAudioWindowSeconds = "stale_audio_window_seconds"
            }
        }
    }

    // MARK: - §3 POST /v1/biometrics — closed-projection batch ingest

    public enum Biometrics {
        /// §3: batch and body caps. ANY violation rejects the WHOLE batch with 400 and
        /// writes ZERO rows — partial acceptance is impossible.
        public static let maxSamplesPerBatch = 500
        public static let maxBodyBytes = 1 * 1024 * 1024 // 1 MiB

        /// §3.1 closed `kind` set (DEC-80(a)).
        public enum Kind: String, Codable {
            case sleepSession = "sleep_session"
            case workout
            case restingHrDaily = "resting_hr_daily"
            case hrvDaily = "hrv_daily"
            case stepsHourly = "steps_hourly"
        }

        /// §3.2 closed `activity` enum. `other` is the deliberate catch-all that keeps
        /// free text out — an unmapped HKWorkoutActivityType projects to `.other`, never
        /// to its Apple name (DEC-80(b)).
        public enum Activity: String, Codable {
            case walking, running, cycling, strength, swimming, hiit, yoga, other
        }

        /// §3.1 `sleep_session` — plausible ranges: 0...1440, 0...1440, 0...200.
        public struct SleepSessionPayload: Codable {
            public let asleepMinutes: Int
            public let inBedMinutes: Int
            public let awakenings: Int

            enum CodingKeys: String, CodingKey {
                case asleepMinutes = "asleep_minutes"
                case inBedMinutes = "in_bed_minutes"
                case awakenings
            }
        }

        /// §3.1 `workout` — plausible ranges: duration 1...86400, energy 0...20000,
        /// hr 20...250, distance 0...500000. `avgHrBpm`/`maxHrBpm`/`distanceMeters` are
        /// the ONLY nullable fields in this schema.
        public struct WorkoutPayload: Codable {
            public let activity: Activity
            public let durationSeconds: Int
            public let activeEnergyKcal: Double
            public let avgHrBpm: Int?
            public let maxHrBpm: Int?
            public let distanceMeters: Double?

            enum CodingKeys: String, CodingKey {
                case activity
                case durationSeconds = "duration_seconds"
                case activeEnergyKcal = "active_energy_kcal"
                case avgHrBpm = "avg_hr_bpm"
                case maxHrBpm = "max_hr_bpm"
                case distanceMeters = "distance_meters"
            }
        }

        /// §3.1 `resting_hr_daily` — plausible range 20...250.
        public struct RestingHrDailyPayload: Codable {
            public let bpm: Int
        }

        /// §3.1 `hrv_daily` — plausible range 1...500.
        public struct HrvDailyPayload: Codable {
            public let sdnnMs: Double

            enum CodingKeys: String, CodingKey {
                case sdnnMs = "sdnn_ms"
            }
        }

        /// §3.1 `steps_hourly` — plausible range 0...100000.
        public struct StepsHourlyPayload: Codable {
            public let steps: Int
        }

        /// Per-kind payload, closed over exactly the §3.1 table. There is no case here
        /// that is not in the spec, and no free-text field anywhere in any case
        /// (DEC-80(b)).
        public enum Payload {
            case sleepSession(SleepSessionPayload)
            case workout(WorkoutPayload)
            case restingHrDaily(RestingHrDailyPayload)
            case hrvDaily(HrvDailyPayload)
            case stepsHourly(StepsHourlyPayload)
        }

        /// §3 one sample. `startedAt`/`endedAt` are ISO-8601 strings with an explicit UTC
        /// offset (§0) — a naive timestamp is a 400 on the wombat side. There is
        /// deliberately NO client-supplied idempotency field: §3.3 derives the dedup key
        /// server-side from kind + the UTC-normalized window + canonical payload JSON.
        public struct Sample: Codable {
            public let kind: Kind
            public let startedAt: String
            public let endedAt: String
            public let payload: Payload

            public init(kind: Kind, startedAt: String, endedAt: String, payload: Payload) {
                self.kind = kind
                self.startedAt = startedAt
                self.endedAt = endedAt
                self.payload = payload
            }

            enum CodingKeys: String, CodingKey {
                case kind
                case startedAt = "started_at"
                case endedAt = "ended_at"
                case payload
            }

            public init(from decoder: Decoder) throws {
                let container = try decoder.container(keyedBy: CodingKeys.self)
                let kind = try container.decode(Kind.self, forKey: .kind)
                self.kind = kind
                self.startedAt = try container.decode(String.self, forKey: .startedAt)
                self.endedAt = try container.decode(String.self, forKey: .endedAt)
                switch kind {
                case .sleepSession:
                    self.payload = .sleepSession(try container.decode(SleepSessionPayload.self, forKey: .payload))
                case .workout:
                    self.payload = .workout(try container.decode(WorkoutPayload.self, forKey: .payload))
                case .restingHrDaily:
                    self.payload = .restingHrDaily(try container.decode(RestingHrDailyPayload.self, forKey: .payload))
                case .hrvDaily:
                    self.payload = .hrvDaily(try container.decode(HrvDailyPayload.self, forKey: .payload))
                case .stepsHourly:
                    self.payload = .stepsHourly(try container.decode(StepsHourlyPayload.self, forKey: .payload))
                }
            }

            public func encode(to encoder: Encoder) throws {
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(kind, forKey: .kind)
                try container.encode(startedAt, forKey: .startedAt)
                try container.encode(endedAt, forKey: .endedAt)
                switch payload {
                case .sleepSession(let p): try container.encode(p, forKey: .payload)
                case .workout(let p): try container.encode(p, forKey: .payload)
                case .restingHrDaily(let p): try container.encode(p, forKey: .payload)
                case .hrvDaily(let p): try container.encode(p, forKey: .payload)
                case .stepsHourly(let p): try container.encode(p, forKey: .payload)
                }
            }
        }

        /// §3 request body.
        public struct BatchRequest: Codable {
            public let v: Int
            public let samples: [Sample]

            public init(samples: [Sample]) {
                self.v = WireContract.wireVersion
                self.samples = samples
            }
        }

        /// §3 response `202`.
        public struct BatchResponse: Codable {
            public let v: Int
            public let accepted: Int
            public let deduplicated: Int
        }
    }

    // MARK: - §4 GET /v1/health — liveness and the format handshake

    public enum Health {
        /// §4 `audio` block. `sampleRateHz` is read from `STREAM_SAMPLE_RATE` on the
        /// wombat side — the SAME constant the Fish request reads. Devices read the rate
        /// HERE and must not hold their own copy of it (TK-358/TK-359 depend on this).
        public struct AudioFormat: Codable {
            public let sampleRateHz: Int
            public let format: String
            public let channels: Int

            enum CodingKeys: String, CodingKey {
                case sampleRateHz = "sample_rate_hz"
                case format, channels
            }
        }

        /// §4 `capabilities` block — reflects the two DEC-78(d) consent toggles as
        /// actually constructed on the wombat side.
        public struct Capabilities: Codable {
            public let remoteVoice: Bool
            public let biometrics: Bool
            public let stream: Bool

            enum CodingKeys: String, CodingKey {
                case remoteVoice = "remote_voice"
                case biometrics, stream
            }
        }

        /// §4 response `200`. `staleAudioWindowSeconds` is the §2 refusal window and
        /// `utteranceTtlSeconds` is the §5 expiry — devices read BOTH here and must not
        /// hold their own copy (this is the drift TK-359 exists to prevent).
        public struct Response: Codable {
            public let v: Int
            public let ok: Bool
            public let deviceId: String
            public let audio: AudioFormat
            public let staleAudioWindowSeconds: Int
            public let utteranceTtlSeconds: Int
            public let capabilities: Capabilities

            enum CodingKeys: String, CodingKey {
                case v, ok
                case deviceId = "device_id"
                case audio
                case staleAudioWindowSeconds = "stale_audio_window_seconds"
                case utteranceTtlSeconds = "utterance_ttl_seconds"
                case capabilities
            }
        }
    }

    // MARK: - §5 GET /v1/utterance — pull the sealed reply

    public enum Utterance {
        /// §5 response headers on the `200` path. The body itself is raw
        /// `pcm_s16le` bytes with NO RIFF header — never JSON.
        public static let utteranceIdHeader = "X-Wombat-Utterance-Id"
        public static let originDeviceIdHeader = "X-Wombat-Origin-Device-Id"
        public static let sampleRateHeader = "X-Wombat-Sample-Rate-Hz"
        public static let audioFormatHeader = "X-Wombat-Audio-Format"
        public static let channelsHeader = "X-Wombat-Channels"
        public static let octetStreamContentType = "application/octet-stream"

        /// Typed view over the §5 response headers. `originDeviceId` is LOAD-BEARING
        /// (DEC-79(c)): the fetching device MUST compare this to its own `device_id` from
        /// §4 and, when they differ, present the reply as a cross-device fallback rather
        /// than an answer to something it said.
        public struct Headers {
            public let utteranceId: String
            public let originDeviceId: String
            public let sampleRateHz: Int
            public let audioFormat: String
            public let channels: Int

            public init?(httpHeaders: [String: String]) {
                guard
                    let utteranceId = httpHeaders[Utterance.utteranceIdHeader],
                    let originDeviceId = httpHeaders[Utterance.originDeviceIdHeader],
                    let sampleRateString = httpHeaders[Utterance.sampleRateHeader],
                    let sampleRateHz = Int(sampleRateString),
                    let audioFormat = httpHeaders[Utterance.audioFormatHeader],
                    let channelsString = httpHeaders[Utterance.channelsHeader],
                    let channels = Int(channelsString)
                else {
                    return nil
                }
                self.utteranceId = utteranceId
                self.originDeviceId = originDeviceId
                self.sampleRateHz = sampleRateHz
                self.audioFormat = audioFormat
                self.channels = channels
            }
        }

        /// §5: `204` is the ORDINARY answer (nothing sealed yet), never an error.
        public static let nothingPendingStatus = 204
        /// §5: single-fetch-then-discard — a successful 200 discards the slot.
        public static let deliveredStatus = 200
    }

    // MARK: - §6 GET /v1/stream (WebSocket upgrade) — the phone fast path

    public enum Stream {
        /// §6: rides the Upgrade request; `X-Wombat-Device-Token` also rides the upgrade
        /// (URLSessionWebSocketTask can set request headers). No token in the query
        /// string, ever.
        public static let subprotocol = "wombat.audio.v1"

        public static let eventUtteranceStart = "utterance_start"
        public static let eventUtteranceEnd = "utterance_end"

        /// §6 framing frame 1 (TEXT).
        public struct UtteranceStartEvent: Codable {
            public let v: Int
            public let event: String
            public let utteranceId: String
            public let originDeviceId: String
            public let sampleRateHz: Int
            public let format: String
            public let channels: Int

            enum CodingKeys: String, CodingKey {
                case v, event
                case utteranceId = "utterance_id"
                case originDeviceId = "origin_device_id"
                case sampleRateHz = "sample_rate_hz"
                case format, channels
            }
        }

        /// §6 framing frame 3 (TEXT). Frame 2 (N BINARY frames of raw pcm_s16le, whole
        /// 2-byte frames only) carries no JSON shape and is not modeled here.
        public struct UtteranceEndEvent: Codable {
            public let v: Int
            public let event: String
            public let utteranceId: String

            enum CodingKeys: String, CodingKey {
                case v, event
                case utteranceId = "utterance_id"
            }
        }
    }

    // MARK: - §8 Pairing QR payload (TK-342 mints it, TK-355 parses it)

    /// §8: the QR encodes exactly this UTF-8 JSON, one line, no whitespace. `token` is
    /// the plaintext per-device token, shown exactly once, and must cross into the
    /// Keychain via `KeychainStore` and nowhere else — never `UserDefaults`, a plist, a
    /// log, or a source constant. `name` is echoed for confirmation only and is NEVER
    /// sent on any request.
    public struct PairingQRPayload: Codable {
        public let v: Int
        public let host: String
        public let port: Int
        public let token: String
        public let name: String
    }
}
