//
//  PairingCoordinator.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  Owns the whole pair-then-probe flow (§8 QR -> Keychain -> Paired): a scanned QR's raw
//  string goes through PairingQRParser (ios/Shared) — the only code path allowed to write
//  the Keychain token — and only THEN does this coordinator run a single liveness probe
//  against the §4 health handshake before it will call itself Paired. A parsed-but-
//  unreachable pairing is explicitly NOT Paired; the probe result is read through the exact
//  same WireContract §0.1 trichotomy every other call site in this app uses.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

@MainActor
public final class PairingCoordinator: ObservableObject {

    public enum State: Equatable {
        case notPaired
        case probing
        case paired(host: String, port: Int, name: String)
        case failed(message: String)
    }

    @Published public private(set) var state: State = .notPaired

    private let account: KeychainStore.Account
    private let session: URLSession

    public init(account: KeychainStore.Account = .phone, session: URLSession = .shared) {
        self.account = account
        self.session = session
    }

    /// Call with the literal text decoded off the scanned QR (§8). Writes the Keychain
    /// token (via PairingQRParser, never directly) BEFORE probing — the probe needs the
    /// token in place to authenticate its own health call.
    public func pair(rawQRPayload: String) async {
        switch PairingQRParser.parse(rawQRPayload, account: account) {
        case .failure(let error):
            state = .failed(message: error.description)
        case .success(let device):
            state = .probing
            await probe(host: device.host, port: device.port, name: device.name)
        }
    }

    /// The §4 liveness probe. Goes through WireContract via the same
    /// HealthHandshakeClient/WombatConnection pair the rest of this app's health-gated
    /// flows use — there is exactly one client type in this app that speaks the health
    /// route, and this coordinator reuses it rather than opening a second one.
    private func probe(host: String, port: Int, name: String) async {
        let connection = WombatConnection(host: host, port: port, account: account)
        let client = HealthHandshakeClient(connection: connection, session: session)

        switch await client.checkHealth() {
        case .unreachable:
            state = .failed(message: "wombat is not reachable on the network right now")
        case .unauthorized:
            state = .failed(message: "this pairing token was not accepted — re-scan the code")
        case .ok(let status, let value):
            guard status == 200, value != nil else {
                state = .failed(message: "wombat did not answer the pairing check")
                return
            }
            state = .paired(host: host, port: port, name: name)
        }
    }
}
