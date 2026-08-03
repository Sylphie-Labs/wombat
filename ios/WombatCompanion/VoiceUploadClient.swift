//
//  VoiceUploadClient.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  Uploads one captured clip through WireContract.Route.voice (§2) — the SAME call both the
//  phone's own hold-to-talk capture (TalkSessionController) and the WatchConnectivity relay
//  (PhoneWatchSession) use. There is exactly one upload path in this app; a watch-relayed
//  clip and a phone-captured clip differ only in where the audio bytes and the
//  X-Wombat-Captured-At value came from, never in how they are sent.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public final class VoiceUploadClient {
    private let connection: WombatConnection
    private let session: URLSession

    public init(connection: WombatConnection, session: URLSession = .shared) {
        self.connection = connection
        self.session = session
    }

    /// `capturedAtHeaderValue` is an already-formatted, ISO-8601-with-offset string.
    /// Callers supply it pre-formatted rather than a `Date` so that a relayed watch clip
    /// (PhoneWatchSession) can forward the watch's original stamp unchanged, with no
    /// reformatting step that could quietly become a re-stamp.
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
