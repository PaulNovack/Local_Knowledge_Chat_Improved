import io
import os
import threading
from typing import Optional
import wave

import numpy as np

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import piper  # type: ignore
from piper.config import SynthesisConfig

APP_PORT = int(os.getenv('PORT', '8001'))
MODEL_PATH = os.getenv('PIPER_MODEL', 'models/en_US-amy-low.onnx')

DEFAULT_LENGTH_SCALE = float(os.getenv('PIPER_LENGTH_SCALE', '1.0'))
DEFAULT_NOISE_SCALE = float(os.getenv('PIPER_NOISE_SCALE', '0.667'))
DEFAULT_NOISE_W = float(os.getenv('PIPER_NOISE_W', '0.8'))

voice = None
synth_lock = threading.Lock()

app = FastAPI(title='Piper TTS HTTP Server')


class SpeakIn(BaseModel):
    text: str
    length_scale: Optional[float] = None
    noise_scale: Optional[float] = None
    noise_w: Optional[float] = None


@app.on_event('startup')
def load_model():
    global voice
    if not os.path.isfile(MODEL_PATH):
        raise RuntimeError(f'Piper model not found at {MODEL_PATH}')
    voice = piper.PiperVoice.load(MODEL_PATH)
    warm = io.BytesIO()
    voice.synthesize('ready', warm)


@app.get('/health')
def health():
    return {'ok': True, 'model': os.path.basename(MODEL_PATH)}


@app.post('/speak')
def speak(payload: SpeakIn):
    if not payload.text or len(payload.text) > 6000:
        raise HTTPException(status_code=400, detail='text is required (<=6000 chars)')

    length_scale = payload.length_scale if payload.length_scale is not None else DEFAULT_LENGTH_SCALE
    noise_scale = payload.noise_scale if payload.noise_scale is not None else DEFAULT_NOISE_SCALE
    noise_w = payload.noise_w if payload.noise_w is not None else DEFAULT_NOISE_W

    try:
        with synth_lock:
            buf = io.BytesIO()
            syn_config = SynthesisConfig(
                length_scale=length_scale,
                noise_scale=noise_scale,
                noise_w_scale=noise_w,
            )
            chunks = list(voice.synthesize(payload.text, syn_config=syn_config))
            if not chunks:
                raise RuntimeError('no audio was generated')
            sample_rate = chunks[0].sample_rate
            audio = np.concatenate([chunk.audio_float_array for chunk in chunks]).astype(np.float32)
            audio = np.clip(audio, -1.0, 1.0)
            pcm = (audio * 32767.0).astype(np.int16)
            with wave.open(buf, 'wb') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(pcm.tobytes())
            buf.seek(0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'synthesis failed: {exc}') from exc

    return StreamingResponse(buf, media_type='audio/wav')


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('server:app', host='0.0.0.0', port=APP_PORT, reload=False)