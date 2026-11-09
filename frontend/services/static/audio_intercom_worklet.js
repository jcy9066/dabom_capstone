// 16 kHz/mono/16-bit signed PCM, packed into 20 ms (640 byte) frames.
// AudioWorklet runs off the browser main thread to avoid UI stalls.
class DabomCaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.samples = new Int16Array(320);
        this.index = 0;
        this.phase = 0;
        this.accumulated = 0;
        this.count = 0;
    }
    process(inputs) {
        const mono = inputs[0]?.[0];
        if (mono) {
            for (let i = 0; i < mono.length; i++) {
                this.accumulated += mono[i];
                this.count++;
                this.phase += 16000;
                if (this.phase >= sampleRate) {
                    this.phase -= sampleRate;
                    const value = Math.max(-1, Math.min(1, this.accumulated / this.count));
                    this.samples[this.index++] = Math.max(-32768, Math.min(32767,
                        Math.round(value * (value < 0 ? 32768 : 32767))));
                    this.accumulated = 0;
                    this.count = 0;
                    if (this.index === 320) {
                        const packet = this.samples.buffer;
                        this.port.postMessage(packet, [packet]);
                        this.samples = new Int16Array(320);
                        this.index = 0;
                    }
                }
            }
        }
        return true;
    }
}
registerProcessor('dabom-capture', DabomCaptureProcessor);
