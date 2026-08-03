//
//  WatchConnection.swift
//  ios/WombatCompanion Watch App
//
//  TK-359 — DRAFT SOURCE (DEC-82 tier A).
//
//  A thin carrier of "where is wombat and which account's token do I use", mirroring
//  ios/WombatCompanion/WombatConnection.swift's shape for this target (that file lives in
//  the phone-only synchronized group and is not visible to this target — see
//  ios/README.md's "Xcode project structure" section on directory-based target
//  membership). It builds NO URL and declares NO header or payload shape of its own — it
//  hands host/port to WireContract.Endpoint and reads the token through KeychainStore,
//  both already owned by ios/Shared.
//
//  The account is fixed to `.watch`, never a parameter: this type exists precisely so the
//  watch can only ever read its OWN token, never the phone's (see WatchTokenReceiver.swift
//  for how that token gets here — a one-shot WatchConnectivity handoff, not a QR scan).
//
//  Where host/port are first obtained is outside this ticket's scope, exactly as the
//  phone-side WombatConnection already punts (its header: "Where host/port are first
//  obtained (pairing) is outside this ticket's scope; this type only wraps values a caller
//  already has."). This type only wraps values a caller already has.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public struct WatchConnection {
    public let endpoint: WireContract.Endpoint

    public init(host: String, port: Int) {
        self.endpoint = WireContract.Endpoint(host: host, port: port)
    }

    /// nil when this watch has never received its token yet (before the one-shot handoff
    /// completes) or after a reset. Read fresh from the Keychain on every access — never
    /// cached in a property that could go stale relative to a revoke.
    public var deviceToken: String? {
        KeychainStore.loadToken(for: .watch)
    }
}
