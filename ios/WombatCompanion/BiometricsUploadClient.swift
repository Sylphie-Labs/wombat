//
//  BiometricsUploadClient.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  Uploads one already-projected batch through WireContract.Route.biometrics (§3). This
//  file authors no route, header or payload shape of its own — everything it sends is a
//  WireContract.Biometrics type, built upstream by BiometricSampleProjection. It mints no
//  per-sample dedup key of any kind; §3.3 derives that key server-side from kind, the UTC
//  window and the canonical payload bytes.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public final class BiometricsUploadClient {
    private let connection: WombatConnection
    private let session: URLSession

    public init(connection: WombatConnection, session: URLSession = .shared) {
        self.connection = connection
        self.session = session
    }

    public func upload(
        samples: [WireContract.Biometrics.Sample]
    ) async -> WireContract.Result<WireContract.Biometrics.BatchResponse> {
        guard let url = connection.endpoint.url(path: WireContract.Route.biometrics) else {
            return .unreachable
        }
        guard let token = connection.deviceToken else {
            return .unauthorized
        }
        guard let body = try? JSONEncoder().encode(WireContract.Biometrics.BatchRequest(samples: samples)) else {
            return .unreachable
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(token, forHTTPHeaderField: WireContract.deviceTokenHeader)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            let (data, response) = try await session.upload(for: request, from: body)
            guard let http = response as? HTTPURLResponse else {
                return .unreachable
            }
            if http.statusCode == 401 {
                return .unauthorized
            }
            let decoded = try? JSONDecoder().decode(WireContract.Biometrics.BatchResponse.self, from: data)
            return .ok(status: http.statusCode, value: decoded)
        } catch {
            return .unreachable
        }
    }
}
