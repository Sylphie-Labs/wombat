//
//  BiometricOfflineBuffer.swift
//  ios/WombatCompanion
//
//  TK-357 — DRAFT SOURCE (DEC-82 tier A).
//
//  Persists PROJECTED WIRE PAYLOAD BYTES — never the underlying HealthKit sample object —
//  captured while the background path cannot reach wombat. Storing the projection's own
//  JSON bytes, encoded once at capture time, is what makes a redelivery re-project
//  byte-identically: wombat's server-derived idempotency key (wire-contract.md §3.3) is a
//  sha256 over kind, the UTC-normalized window and canonical sorted-key payload JSON, so
//  replaying a fresh projection at drain time — rather than resending what was already
//  projected — is exactly the kind of drift that would mint a NEW key and land as a
//  duplicate row instead of a dedup. There is no client-supplied idempotency field written
//  or read anywhere in this file; §3.3 is server-derived only.
//
//  ORDERING: entries live in a plain array, appended at the tail and drained from the
//  head — oldest first, FIFO. This is a deliberate explicit order, not incidental
//  dictionary iteration (Foundation dictionaries make no ordering guarantee), because the
//  drain-on-reconnect contract (TK-357 AC2) depends on samples reaching wombat in the
//  order they were captured.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import Foundation

public enum BiometricOfflineBuffer {
    private static let defaultsKey = "wombat.biometrics.offlineBuffer.v1"
    private static let defaults = UserDefaults.standard

    /// Appends already-projected samples to the tail of the buffer, encoding each one to
    /// its own wire JSON bytes right now — at capture time, not at drain time — so what is
    /// persisted is exactly the bytes a later drain will resend.
    public static func enqueue(_ samples: [WireContract.Biometrics.Sample]) {
        guard !samples.isEmpty else { return }
        let encoder = JSONEncoder()
        let newEntries = samples.compactMap { try? encoder.encode($0) }
        var stored = loadRaw()
        stored.append(contentsOf: newEntries)
        defaults.set(stored, forKey: defaultsKey)
    }

    /// The full buffer, oldest first, decoded back into wire samples. This IS the ordered
    /// drain (TK-357 AC2) — head-to-tail array order, never a dictionary's iteration
    /// order.
    public static func drainOrdered() -> [WireContract.Biometrics.Sample] {
        let decoder = JSONDecoder()
        return loadRaw().compactMap { try? decoder.decode(WireContract.Biometrics.Sample.self, from: $0) }
    }

    /// Call ONLY after wombat has confirmed (a §3 response `202`) that the first `count`
    /// entries — in the same oldest-first order `drainOrdered()` returned them — were
    /// accepted. Removes exactly those from the head of the buffer; anything appended
    /// concurrently, or left over from a partial batch, stays queued for the next drain.
    public static func removeDrained(count: Int) {
        guard count > 0 else { return }
        var stored = loadRaw()
        guard !stored.isEmpty else { return }
        stored.removeFirst(min(count, stored.count))
        defaults.set(stored, forKey: defaultsKey)
    }

    public static var count: Int {
        loadRaw().count
    }

    /// TK-357's app-side reset (BiometricDeviceReset) calls this alongside clearing every
    /// per-type anchor, so the next sync re-reads from the anchorless start on both sides
    /// of a DEC-75 wipe.
    public static func clear() {
        defaults.removeObject(forKey: defaultsKey)
    }

    private static func loadRaw() -> [Data] {
        defaults.array(forKey: defaultsKey) as? [Data] ?? []
    }
}
