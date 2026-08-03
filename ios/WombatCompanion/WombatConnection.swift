//
//  WombatConnection.swift
//  ios/WombatCompanion
//
//  TK-358 — DRAFT SOURCE (DEC-82 tier A).
//
//  A thin carrier of "where is wombat and which account's token do I use" for the phone
//  target's clients (HealthHandshakeClient, VoiceUploadClient, StreamPlaybackClient). It
//  builds NO URL and declares NO header or payload shape of its own — it hands host/port to
//  WireContract.Endpoint and reads the token through KeychainStore, both already owned by
//  ios/Shared. Where host/port are first obtained (pairing) is outside this ticket's scope;
//  this type only wraps values a caller already has.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public struct WombatConnection {
    public let endpoint: WireContract.Endpoint
    public let account: KeychainStore.Account

    public init(host: String, port: Int, account: KeychainStore.Account = .phone) {
        self.endpoint = WireContract.Endpoint(host: host, port: port)
        self.account = account
    }

    /// nil when this account has never been paired (or was deleted on revoke/reset).
    public var deviceToken: String? {
        KeychainStore.loadToken(for: account)
    }
}
