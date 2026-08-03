//
//  HealthHandshakeClient.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  Calls WireContract.Route.health (the §4 liveness + format handshake) and hands back the
//  decoded response — including audio.sampleRateHz, the ONE place a sample rate is meant to
//  enter this app. Nothing downstream (TalkSessionController, StreamPlaybackClient) is
//  allowed to hold a numeric sample-rate literal of its own; every consumer reads the rate
//  off the value this client returns, or off a later utterance_start frame — also sourced
//  through WireContract.
//
//  Every URL and header this file touches comes from WireContract (ios/Shared/WireContract.swift).
//  This file authors no route, no header name and no payload shape of its own.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

/// Performs the format handshake and reports the WireContract result trichotomy (§0.1) —
/// callers MUST branch on all three cases; collapsing `.unreachable` and `.unauthorized` is
/// the exact defect the trichotomy exists to prevent.
public final class HealthHandshakeClient {
    private let connection: WombatConnection
    private let session: URLSession

    public init(connection: WombatConnection, session: URLSession = .shared) {
        self.connection = connection
        self.session = session
    }

    public func checkHealth() async -> WireContract.Result<WireContract.Health.Response> {
        guard let url = connection.endpoint.url(path: WireContract.Route.health) else {
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
            let decoded = try? JSONDecoder().decode(WireContract.Health.Response.self, from: data)
            return .ok(status: http.statusCode, value: decoded)
        } catch {
            return .unreachable
        }
    }
}
