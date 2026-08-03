//
//  WatchVoiceUploadClient.swift
//  ios/WombatCompanion Watch App
//
//  TK-359 — DRAFT SOURCE (DEC-82 tier A).
//
//  The PRIMARY delivery path (DEC-82 tier A / the research finding this ticket exists to
//  express in source): a direct POST through WireContract.Route.voice (§2) over the
//  watch's own Wi-Fi, authenticated with the watch's OWN token from KeychainStore(.watch)
//  — never the phone's, and never fetched from the phone at call time. This client makes
//  no WatchConnectivity call of any kind; it is a plain HTTP client exactly like the
//  phone's own VoiceUploadClient, just pointed at the watch's own connection/token.
//
//  See WatchTalkSessionController for where this is tried FIRST and the WatchRelaySession
//  fallback is reached only on this client's failure — the ordering that must be visible
//  in the source rather than emergent.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public final class WatchVoiceUploadClient {
    private let connection: WatchConnection
    private let session: URLSession

    public init(connection: WatchConnection, session: URLSession = .shared) {
        self.connection = connection
        self.session = session
    }

    /// `capturedAtHeaderValue` is an already-formatted, ISO-8601-with-offset string — the
    /// moment the hold began, i.e. the moment the mic opened. Callers supply it pre-formatted
    /// so there is no reformatting step between capture and send that could quietly become a
    /// re-stamp (the same discipline PhoneWatchSession's relay forwarding follows).
    public func upload(
        audioData: Data,
        capturedAtHeaderValue: String
    ) async -> WireContract.Result<WireContract.Voice.AcceptedResponse> {
        guard let url = connection.endpoint.url(path: WireContract.Route.voice) else {
            return .unreachable
        }
        guard let token = connection.deviceToken else {
            return .unauthorized
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(token, forHTTPHeaderField: WireContract.deviceTokenHeader)
        request.setValue(WireContract.Voice.contentType, forHTTPHeaderField: "Content-Type")
        request.setValue(capturedAtHeaderValue, forHTTPHeaderField: WireContract.Voice.capturedAtHeader)

        do {
            let (data, response) = try await session.upload(for: request, from: audioData)
            guard let http = response as? HTTPURLResponse else {
                return .unreachable
            }
            if http.statusCode == 401 {
                return .unauthorized
            }
            let decoded = try? JSONDecoder().decode(WireContract.Voice.AcceptedResponse.self, from: data)
            return .ok(status: http.statusCode, value: decoded)
        } catch {
            return .unreachable
        }
    }
}
