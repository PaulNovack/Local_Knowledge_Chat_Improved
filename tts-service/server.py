import os
import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

TTS_PORT = int(os.getenv('TTS_PORT', '8001'))
TTS_VOICE = os.getenv('TTS_VOICE', 'en-us')
TTS_RATE = int(os.getenv('TTS_RATE', '175'))
TTS_PITCH = int(os.getenv('TTS_PITCH', '50'))
TTS_BINARY = os.getenv('TTS_BINARY')


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6000)


def _tts_command():
    binary = TTS_BINARY or shutil.which('espeak-ng') or shutil.which('espeak')
    if not binary:
        raise RuntimeError('No speech synthesis binary found. Install espeak-ng or set TTS_BINARY.')
    return binary


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.tts_binary = _tts_command()
    yield


app = FastAPI(title='Local TTS Service', lifespan=lifespan)


@app.get('/health')
def health():
    return {
        'ok': True,
        'engine': os.path.basename(app.state.tts_binary),
        'voice': TTS_VOICE,
        'rate': TTS_RATE,
        'pitch': TTS_PITCH,
    }


@app.post('/speak')
def speak(payload: SpeakRequest):
    safe_text = payload.text.strip()
    if not safe_text:
        raise HTTPException(status_code=400, detail='text is required')

    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
        wav_path = tmp.name

    try:
        subprocess.run(
            [
                app.state.tts_binary,
                '-v', TTS_VOICE,
                '-s', str(TTS_RATE),
                '-p', str(TTS_PITCH),
                '-w', wav_path,
                safe_text,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        with open(wav_path, 'rb') as stream:
            audio = stream.read()
        return Response(
            content=audio,
            media_type='audio/wav',
            headers={'content-disposition': 'inline; filename="reply.wav"'},
        )
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=f'TTS failed: {exc.stderr.strip() or exc}') from exc
    finally:
        try:
            os.remove(wav_path)
        except Exception:
            pass


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('server:app', host='0.0.0.0', port=TTS_PORT, reload=False)