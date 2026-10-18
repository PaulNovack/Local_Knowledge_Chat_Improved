import io
import os
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import piper  # type: ignore

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
            voice.synthesize(
                payload.text,
                buf,
                length_scale=length_scale,
                noise_scale=noise_scale,
                noise_w=noise_w,
            )
            buf.seek(0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'synthesis failed: {exc}') from exc

    return StreamingResponse(buf, media_type='audio/wav')


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('server:app', host='0.0.0.0', port=APP_PORT, reload=False)