//
//  WatchUtterancePlaybackClient.swift
//  ios/WombatCompanion Watch App
//
//  TK-360 — DRAFT SOURCE (DEC-82 tier A, DEC-83).
//
//  PULL, NEVER PUSH. This file makes exactly one HTTP call — a GET through
//  WireContract.Route.utterance (§5) — and nothing else. There is no socket accept, no
//  inbound connection and no long-lived transport of any kind here: this device opens the
//  connection, wombat answers, the connection closes. That is the whole shape, and it is
//  what keeps DEC-78(e)'s direction claim true by construction rather than by policy.
//
//  §5's `204` is decoded here as an ordinary `.ok(status: 204, value: nil)` — ROUTE-LEVEL
//  parity with every other route in this tree, not a distinguished error case. Retrying it
//  within the TTL is WatchUtterancePlaybackController's job, not this file's; this file
//  answers ONE question per call — "is a sealed reply waiting right now" — and returns.
//
//  Buffer, never stream: a `200` reads the WHOLE body into `Data` before returning
//  (`session.data(for:)`, not a byte-by-byte reader), matching TK-360's non_goals — no live
//  streaming, no chunked playback.
//
//  Every URL and header this file touches comes from WireContract
//  (ios/Shared/WireContract.swift). This file authors no route, no header name and no
//  payload shape of its own.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public final class WatchUtterancePlaybackClient {

    /// A delivered `200` — the raw §5 headers plus the buffered PCM body they describe.
    public struct Payload {
        public let headers: WireContract.Utterance.Headers
        public let audioData: Data
    }

    private let connection: WatchConnection
    private let session: URLSession

    public init(connection: WatchConnection, session: URLSession = .shared) {
        self.connection = connection
        self.session = session
    }

    /// One authenticated GET. Callers MUST branch on all three WireContract.Result cases
    /// (§0.1) — `.ok(status: 204, value: nil)` is the ordinary "nothing sealed yet" answer
    /// and must never be treated the same as `.unreachable`.
    public func fetchUtterance() async -> WireContract.Result<Payload> {
        guard let url = connection.endpoint.url(path: WireContract.Route.utterance) else {
            return .unreachable
        }
        guard let token = connection.deviceToken else {
            return .unauthorized
        }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue(token, forHTTPHeaderField: WireContract.deviceTokenHeader)

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .unreachable
            }
            if http.statusCode == 401 {
                return .unauthorized
            }
            guard http.statusCode == WireContract.Utterance.deliveredStatus else {
                // §5 `204` (or anything else that is not the delivered status) carries no
                // body worth decoding — the ordinary case, reported with a nil value.
                return .ok(status: http.statusCode, value: nil)
            }

            var httpHeaders: [String: String] = [:]
            for (key, value) in http.allHeaderFields {
                if let keyString = key as? String, let valueString = value as? String {
                    httpHeaders[keyString] = valueString
                }
            }
            guard let headers = WireContract.Utterance.Headers(httpHeaders: httpHeaders) else {
                // A `200` missing the §5 header set is not a shape this client can trust
                // enough to play — reported as "nothing usable" rather than guessed at.
                return .ok(status: http.statusCode, value: nil)
            }
            return .ok(status: http.statusCode, value: Payload(headers: headers, audioData: data))
        } catch {
            return .unreachable
        }
    }
}
