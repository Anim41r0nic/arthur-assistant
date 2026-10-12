// El micrófono de la web (fase S5, paso 2). Corre en el hilo de audio del navegador (AudioWorklet):
// copia cada trocito del canal 0 y se lo pasa a la página (chat.js), que lo junta, lo pasa a 16 kHz y lo
// envía al servidor como WAV. Aquí no se guarda ni se envía nada por la red.
class ArthurMic extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) this.port.postMessage(channel.slice(0));
    return true;  // seguir vivo hasta que la página desconecte el micro
  }
}
registerProcessor("arthur-mic", ArthurMic);
