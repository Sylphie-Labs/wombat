/**
 * TK-224 (Q-111(b) ruled shape): pure/testable mic-capture pieces feeding the
 * EXISTING ASR drop-dir - `encodeWav` is a pure PCM encoder; `MicCapture`
 * wraps Web Audio (`getUserMedia` + `AudioContext` + `ScriptProcessorNode` -
 * NEVER `MediaRecorder`, which emits webm/opus, a suffix `ASRSource`'s scan
 * rejects) and hands a finished capture to `window.wombatAudio.saveCapture`
 * (the preload bridge, `app/electron/preload.ts`) - the renderer never
 * writes to the filesystem itself, and the actual drop-dir resolution lives
 * ENTIRELY in the main process (`app/electron/save-capture.ts`).
 */

const PCM_BUFFER_SIZE = 4096;
const CHANNELS = 1;
const BITS_PER_SAMPLE = 16;

export interface AudioCaptureDevice {
  readonly deviceId: string;
  readonly label: string;
}

export interface SaveCaptureResult {
  readonly ok: boolean;
  readonly path?: string;
  readonly reason?: "drop-dir-not-configured" | "write-failed";
}

declare global {
  interface Window {
    wombatAudio: {
      saveCapture(buffer: ArrayBuffer): Promise<SaveCaptureResult>;
    };
  }
}

/** Input (microphone) devices only - `enumerateDevices()`'s `audiooutput`/`videoinput` kinds
 * are filtered out. Labels are blank until mic permission has been granted at least once;
 * that's a browser/OS concern, not something this module works around. */
export async function listInputDevices(): Promise<AudioCaptureDevice[]> {
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices
    .filter((device) => device.kind === "audioinput")
    .map((device, index) => ({
      deviceId: device.deviceId,
      label: device.label || `Microphone ${index + 1}`,
    }));
}

function mergeChunks(chunks: readonly Float32Array[]): Float32Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const merged = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged;
}

/**
 * Encodes mono 32-bit float PCM samples (each in `[-1, 1]`, out-of-range
 * values clamped) into a canonical 16-bit PCM RIFF/WAVE `ArrayBuffer` at
 * `sampleRate` - the capture's native rate is used as-is (faster-whisper
 * resamples; no resampling happens here).
 */
export function encodeWav(samples: Float32Array, sampleRate: number): ArrayBuffer {
  const bytesPerSample = BITS_PER_SAMPLE / 8;
  const blockAlign = CHANNELS * bytesPerSample;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  function writeString(offset: number, value: string): void {
    for (let i = 0; i < value.length; i++) {
      view.setUint8(offset + i, value.charCodeAt(i));
    }
  }

  writeString(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size (PCM)
  view.setUint16(20, 1, true); // audio format: PCM
  view.setUint16(22, CHANNELS, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true); // byte rate
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, BITS_PER_SAMPLE, true);
  writeString(36, "data");
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    const scaled = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    view.setInt16(offset, Math.round(scaled), true);
    offset += 2;
  }

  return buffer;
}

export type CaptureStopResult =
  | { readonly kind: "saved"; readonly result: SaveCaptureResult }
  // Nothing was ever captured (e.g. the WHOLE session was muted) - the
  // hand-off is never reached (AC2: while muted, nothing is delivered).
  | { readonly kind: "empty" };

/**
 * One recording session: `getUserMedia` -> `AudioContext` +
 * `ScriptProcessorNode` PCM collection -> `encodeWav` -> `saveCapture`.
 * `setMuted` BOTH disables the underlying media track AND suppresses PCM
 * collection - while muted, no sample ever reaches the hand-off.
 */
export class MicCapture {
  private stream: MediaStream | null = null;
  private audioContext: AudioContext | null = null;
  private processor: ScriptProcessorNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private chunks: Float32Array[] = [];
  private muted = false;

  /** `deviceId` (if given) constrains capture to that input - applied fresh on every
   * `start()`, so a device-selector change only takes effect on the NEXT capture. */
  async start(deviceId?: string, initialMuted = false): Promise<void> {
    this.muted = initialMuted;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: deviceId ? { deviceId: { exact: deviceId } } : true,
    });
    this.applyMuteToTracks();

    this.audioContext = new window.AudioContext();
    this.source = this.audioContext.createMediaStreamSource(this.stream);
    this.processor = this.audioContext.createScriptProcessor(PCM_BUFFER_SIZE, CHANNELS, CHANNELS);
    this.chunks = [];
    this.processor.onaudioprocess = (event: AudioProcessingEvent) => {
      if (this.muted) return;
      this.chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
    };
    this.source.connect(this.processor);
    this.processor.connect(this.audioContext.destination);
  }

  private applyMuteToTracks(): void {
    for (const track of this.stream?.getAudioTracks() ?? []) {
      track.enabled = !this.muted;
    }
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.applyMuteToTracks();
  }

  /** Tears down the capture graph and, if any unmuted PCM was collected, hands the encoded
   * WAV to `window.wombatAudio.saveCapture`. Returns `{kind: "empty"}` without ever calling
   * `saveCapture` when nothing was captured. */
  async stop(): Promise<CaptureStopResult> {
    this.processor?.disconnect();
    this.source?.disconnect();
    for (const track of this.stream?.getAudioTracks() ?? []) {
      track.stop();
    }
    const sampleRate = this.audioContext?.sampleRate ?? 16000;
    await this.audioContext?.close();
    this.audioContext = null;
    this.processor = null;
    this.source = null;
    this.stream = null;

    if (this.chunks.length === 0) {
      return { kind: "empty" };
    }
    const merged = mergeChunks(this.chunks);
    this.chunks = [];
    const wav = encodeWav(merged, sampleRate);
    const result = await window.wombatAudio.saveCapture(wav);
    return { kind: "saved", result };
  }
}
