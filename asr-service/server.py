import os
import tempfile
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
import av


_av_open = av.open


def _open_compat(*args, **kwargs):
    kwargs.pop('metadata_errors', None)
    return _av_open(*args, **kwargs)


av.open = _open_compat

from faster_whisper import WhisperModel

ASR_MODEL = os.getenv('ASR_MODEL', 'small')
ASR_DEVICE = os.getenv('ASR_DEVICE', 'cpu')
ASR_COMPUTE = os.getenv('ASR_COMPUTE', 'int8')
ASR_LANG = os.getenv('ASR_LANG', 'en')
ASR_PORT = int(os.getenv('ASR_PORT', '9000'))
ASR_CPU_THREADS = int(os.getenv('ASR_CPU_THREADS', '4'))
ASR_NUM_WORKERS = int(os.getenv('ASR_NUM_WORKERS', '1'))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.model = WhisperModel(
        ASR_MODEL,
        device=ASR_DEVICE,
        compute_type=ASR_COMPUTE,
        cpu_threads=ASR_CPU_THREADS,
        num_workers=ASR_NUM_WORKERS,
    )
    try:
        yield
    finally:
        try:
            del app.state.model
        except Exception:
            pass


app = FastAPI(title='Faster-Whisper Service', lifespan=lifespan)


@app.get('/health')
def health():
    return {
        'ok': True,
        'model': ASR_MODEL,
        'device': ASR_DEVICE,
        'compute': ASR_COMPUTE,
        'cpu_threads': ASR_CPU_THREADS,
        'num_workers': ASR_NUM_WORKERS,
    }


@app.post('/transcribe')
async def transcribe(audio: UploadFile = File(...)):
    if not audio or not audio.filename:
        raise HTTPException(status_code=400, detail='No audio file')

    suffix = f"_{os.path.basename(audio.filename)}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    started = time.time()
    try:
        segments, info = app.state.model.transcribe(
            tmp_path,
            language=ASR_LANG,
            vad_filter=True,
        )
        text = ''.join(segment.text for segment in segments).strip()
        return JSONResponse({
            'text': text,
            'time_ms': int((time.time() - started) * 1000),
            'language': getattr(info, 'language', None),
            'language_probability': getattr(info, 'language_probability', None),
        })
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'ASR failed: {exc}') from exc
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('server:app', host='0.0.0.0', port=ASR_PORT, reload=False)