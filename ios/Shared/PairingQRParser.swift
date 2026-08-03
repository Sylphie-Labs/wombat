//
//  PairingQRParser.swift
//  ios/Shared
//
//  TK-355 — DRAFT SOURCE FOUNDATION (DEC-82 tier A, DEC-83 §8).
//
//  Parses the §8 pairing QR JSON (minted by TK-342) into one typed value and writes the
//  token through KeychainStore — the single wrapper, never a second home for a token.
//  A QR whose "v" is not 1 is rejected with a plain, spoken-language message. Never a
//  crash, never a silent partial parse.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public enum PairingQRParser {

    /// The non-secret fields surfaced back to the caller for display/confirmation. The
    /// token itself never leaves KeychainStore's custody once parsed.
    public struct PairedDevice {
        public let host: String
        public let port: Int
        public let name: String
    }

    public enum ParseError: Error, CustomStringConvertible {
        /// §8: "A QR whose v is not 1 is rejected with a plain ... message."
        case unsupportedVersion(Int)
        /// Not valid UTF-8 / not valid JSON / missing a required §8 field.
        case malformed
        case keychainWriteFailed

        public var description: String {
            switch self {
            case .unsupportedVersion:
                return "this pairing code is from a different version of wombat"
            case .malformed:
                return "this pairing code could not be read"
            case .keychainWriteFailed:
                return "this device's pairing token could not be saved"
            }
        }
    }

    /// Decodes `rawJSON` (the literal QR payload text), validates the wire version, and
    /// — only on success — writes `payload.token` to the Keychain for `account`. Never
    /// throws; the trichotomy of outcomes is expressed as a `Result` so a call site
    /// cannot forget to handle the "wrong version" case as anything other than a plain
    /// message.
    public static func parse(
        _ rawJSON: String,
        account: KeychainStore.Account
    ) -> Result<PairedDevice, ParseError> {
        guard let data = rawJSON.data(using: .utf8) else {
            return .failure(.malformed)
        }

        let payload: WireContract.PairingQRPayload
        do {
            payload = try JSONDecoder().decode(WireContract.PairingQRPayload.self, from: data)
        } catch {
            return .failure(.malformed)
        }

        guard payload.v == WireContract.wireVersion else {
            return .failure(.unsupportedVersion(payload.v))
        }

        do {
            try KeychainStore.saveToken(payload.token, for: account)
        } catch {
            return .failure(.keychainWriteFailed)
        }

        return .success(PairedDevice(host: payload.host, port: payload.port, name: payload.name))
    }
}
