// AudioWorklet processor for the streaming mic: forwards mono PCM
// frames to the main thread, buffered to ~4096 samples per message
// so the message rate stays sane. The main thread resamples to
// 24 kHz int16 and posts chunks to /api/transcribe_stream/audio.
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = [];
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) this.buf.push(ch[i]);
      if (this.buf.length >= 4096) {
        this.port.postMessage(new Float32Array(this.buf));
        this.buf = [];
      }
    }
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);
