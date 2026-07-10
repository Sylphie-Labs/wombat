import { describe, expect, it } from "vitest";

import { encodeWav } from "./audio";

/**
 * TK-224 AC1: encodeWav's output is parsed back with a tiny, independent
 * reader (no shared parsing code with the encoder) - a valid RIFF/WAVE
 * header, PCM format, 16-bit depth, correct data length, and a sample-rate
 * round trip.
 */

function readWavHeader(buffer: ArrayBuffer): {
  riff: string;
  wave: string;
  fmtId: string;
  audioFormat: number;
  numChannels: number;
  sampleRate: number;
  bitsPerSample: number;
  dataId: string;
  dataSize: number;
} {
  const view = new DataView(buffer);
  const text = (offset: number, length: number): string =>
    String.fromCharCode(...new Uint8Array(buffer, offset, length));
  return {
    riff: text(0, 4),
    wave: text(8, 4),
    fmtId: text(12, 4),
    audioFormat: view.getUint16(20, true),
    numChannels: view.getUint16(22, true),
    sampleRate: view.getUint32(24, true),
    bitsPerSample: view.getUint16(34, true),
    dataId: text(36, 4),
    dataSize: view.getUint32(40, true),
  };
}

describe("encodeWav", () => {
  it("produces a canonical 16-bit PCM mono RIFF/WAVE header with the data length and sample rate round-tripping", () => {
    const samples = new Float32Array([0, 0.5, -0.5, 1, -1]);
    const buffer = encodeWav(samples, 16000);
    const header = readWavHeader(buffer);

    expect(header.riff).toBe("RIFF");
    expect(header.wave).toBe("WAVE");
    expect(header.fmtId).toBe("fmt ");
    expect(header.audioFormat).toBe(1); // PCM
    expect(header.numChannels).toBe(1);
    expect(header.sampleRate).toBe(16000);
    expect(header.bitsPerSample).toBe(16);
    expect(header.dataId).toBe("data");
    expect(header.dataSize).toBe(samples.length * 2);
    expect(buffer.byteLength).toBe(44 + samples.length * 2);
  });

  it("round-trips at a different sample rate and sample count", () => {
    const samples = new Float32Array(100).fill(0.1);
    const buffer = encodeWav(samples, 48000);
    const header = readWavHeader(buffer);

    expect(header.sampleRate).toBe(48000);
    expect(header.dataSize).toBe(200);
    expect(buffer.byteLength).toBe(244);
  });

  it("clamps out-of-range samples to the 16-bit PCM extremes", () => {
    const samples = new Float32Array([1, -1, 2, -2]);
    const buffer = encodeWav(samples, 8000);
    const view = new DataView(buffer);
    const readInt16 = (i: number): number => view.getInt16(44 + i * 2, true);

    expect(readInt16(0)).toBe(0x7fff);
    expect(readInt16(1)).toBe(-0x8000);
    expect(readInt16(2)).toBe(0x7fff); // 2 clamped to the same max as 1
    expect(readInt16(3)).toBe(-0x8000); // -2 clamped to the same min as -1
  });

  it("encodes silence as all-zero data bytes", () => {
    const buffer = encodeWav(new Float32Array([0, 0, 0]), 16000);
    const view = new DataView(buffer);
    for (let i = 0; i < 3; i++) {
      expect(view.getInt16(44 + i * 2, true)).toBe(0);
    }
  });
});
