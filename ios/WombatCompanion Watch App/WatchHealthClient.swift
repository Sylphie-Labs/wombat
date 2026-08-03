//
//  WatchHealthClient.swift
//  ios/WombatCompanion Watch App
//
//  TK-359 — DRAFT SOURCE (DEC-82 tier A).
//
//  Calls WireContract.Route.health (§4) directly over the watch's own Wi-Fi and hands back
//  the decoded response — including staleAudioWindowSeconds, the ONE place a staleness
//  window is meant to enter this target. WatchTalkSessionController reads the window off
//  the value THIS client returns and holds no numeric copy of its own (this is the drift
//  TK-359 exists to prevent — see WatchTalkSessionController's staleness check).
//
//  Every URL and header this file touches comes from WireContract
//  (ios/Shared/WireContract.swift). This file authors no route, no header name and no
//  payload shape of its own.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

/// Performs the format/staleness handshake and reports the WireContract result trichotomy
/// (§0.1) — callers MUST branch on all three cases; collapsing `.unreachable` and
/// `.unauthorized` is the exact defect the trichotomy exists to prevent.
public final class WatchHealthClient {
    private let connection: WatchConnection
    private let session: URLSession

    public init(connection: WatchConnection, session: URLSession = .shared) {
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
