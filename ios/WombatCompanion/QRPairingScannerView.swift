//
//  QRPairingScannerView.swift
//  ios/WombatCompanion
//
//  TK-356 — DRAFT SOURCE (DEC-82 tier A).
//
//  The camera-facing half of §8 pairing: decodes a scanned QR symbol's raw string and hands
//  it to PairingCoordinator.pair(rawQRPayload:), which owns everything after that (Keychain
//  write, probe, Paired state). This file authors no parsing, no Keychain access and no
//  network call of its own.
//
//  Info.plist NOTE (not this ticket's file to edit — see ios/WombatCompanion/Info.plist):
//  a camera-facing screen needs NSCameraUsageDescription declared before it can run on
//  hardware. TK-355 pre-declared the HealthKit and local-network descriptions this ticket
//  needed the same way; this one is left as a flagged gap for whichever tier-A/tier-B pass
//  next touches that file, the same pattern TK-355 used for TK-356's own HealthKit keys.
//
//  THIS SOURCE HAS NEVER BEEN COMPILED. See ios/README.md.
//

import AVFoundation
import SwiftUI
import UIKit

public struct QRPairingScannerView: UIViewControllerRepresentable {
    @ObservedObject private var pairingCoordinator: PairingCoordinator

    public init(pairingCoordinator: PairingCoordinator) {
        self.pairingCoordinator = pairingCoordinator
    }

    public func makeCoordinator() -> ScanBridge {
        ScanBridge(pairingCoordinator: pairingCoordinator)
    }

    public func makeUIViewController(context: Context) -> ScannerViewController {
        let controller = ScannerViewController()
        controller.delegate = context.coordinator
        return controller
    }

    public func updateUIViewController(_ uiViewController: ScannerViewController, context: Context) {}

    /// Bridges AVFoundation's delegate callback into an async call on PairingCoordinator.
    /// The in-flight guard stops a still-open camera pointed at the same code from firing a
    /// second pair(rawQRPayload:) while the first probe is still running.
    public final class ScanBridge: NSObject, ScannerViewControllerDelegate {
        private let pairingCoordinator: PairingCoordinator
        private var isHandlingScan = false

        init(pairingCoordinator: PairingCoordinator) {
            self.pairingCoordinator = pairingCoordinator
        }

        public func scanner(_ controller: ScannerViewController, didScan payload: String) {
            guard !isHandlingScan else { return }
            isHandlingScan = true
            Task { @MainActor in
                await pairingCoordinator.pair(rawQRPayload: payload)
                isHandlingScan = false
            }
        }
    }
}

public protocol ScannerViewControllerDelegate: AnyObject {
    func scanner(_ controller: ScannerViewController, didScan payload: String)
}

/// Thin AVCaptureMetadataOutput wrapper — the entire camera/QR-decode surface in this app.
/// It owns no parsing beyond "this is a QR symbol, here is its string value";
/// PairingQRParser (ios/Shared) does the rest.
public final class ScannerViewController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    public weak var delegate: ScannerViewControllerDelegate?

    private let captureSession = AVCaptureSession()

    public override func viewDidLoad() {
        super.viewDidLoad()
        configureSession()
    }

    public override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        if !captureSession.isRunning {
            captureSession.startRunning()
        }
    }

    public override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        if captureSession.isRunning {
            captureSession.stopRunning()
        }
    }

    private func configureSession() {
        guard
            let captureDevice = AVCaptureDevice.default(for: .video),
            let input = try? AVCaptureDeviceInput(device: captureDevice),
            captureSession.canAddInput(input)
        else {
            return
        }
        captureSession.addInput(input)

        let output = AVCaptureMetadataOutput()
        guard captureSession.canAddOutput(output) else { return }
        captureSession.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        output.metadataObjectTypes = [.qr]

        let previewLayer = AVCaptureVideoPreviewLayer(session: captureSession)
        previewLayer.videoGravity = .resizeAspectFill
        previewLayer.frame = view.bounds
        view.layer.addSublayer(previewLayer)
    }

    public func metadataOutput(
        _ output: AVCaptureMetadataOutput,
        didOutput metadataObjects: [AVMetadataObject],
        from connection: AVCaptureConnection
    ) {
        guard
            let readable = metadataObjects.first as? AVMetadataMachineReadableCodeObject,
            readable.type == .qr,
            let payload = readable.stringValue
        else {
            return
        }
        delegate?.scanner(self, didScan: payload)
    }
}
