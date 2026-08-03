//
//  KeychainStore.swift
//  ios/Shared
//
//  TK-355 — DRAFT SOURCE FOUNDATION (DEC-82 tier A, DEC-83 §8).
//
//  The ONLY code path in this tree that is allowed to persist a pairing token. §8's
//  PairingQRPayload.token comes out of the QR and MUST cross this wrapper into the
//  Keychain — never UserDefaults, a plist, a log line, or a source constant.
//
//  Both targets use this store: the phone for its own device token (TK-356) and the
//  watch for its separately-minted token received over the one-shot WatchConnectivity
//  handoff (TK-359). One wrapper, two accounts, never a third home for a token.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation
import Security

public enum KeychainStore {

    private static let service = "com.wombat.companion.devicetoken"

    /// Which device's token a given save/load/delete call addresses. `.phone` is written
    /// by the phone's own QR pairing flow (TK-356); `.watch` is written by the watch on
    /// receipt of its own token over WatchConnectivity (TK-359) — the watch never reuses
    /// the phone's token.
    public enum Account: String {
        case phone
        case watch
    }

    public enum KeychainError: Error {
        case unableToSave(OSStatus)
        case unableToDelete(OSStatus)
    }

    private static func query(for account: Account) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account.rawValue,
        ]
    }

    /// Overwrites any existing token for `account`.
    public static func saveToken(_ token: String, for account: Account) throws {
        let base = query(for: account)
        SecItemDelete(base as CFDictionary)

        var attributes = base
        attributes[kSecValueData as String] = Data(token.utf8)
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock

        let status = SecItemAdd(attributes as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainError.unableToSave(status)
        }
    }

    /// Returns nil when no token has been saved for `account` (e.g. not yet paired).
    public static func loadToken(for account: Account) -> String? {
        var attributes = query(for: account)
        attributes[kSecReturnData as String] = true
        attributes[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(attributes as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    /// Clears a token — e.g. on revoke, on re-pair, or as part of the DEC-75 app-side
    /// reset (TK-357) so the wipe promise reaches the device side too.
    public static func deleteToken(for account: Account) throws {
        let status = SecItemDelete(query(for: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unableToDelete(status)
        }
    }
}
