FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p models \
 && curl -L --fail -o models/en_US-amy-low.onnx \
    https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/low/en_US-amy-low.onnx \
 && curl -L --fail -o models/en_US-amy-low.onnx.json \
    https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/low/en_US-amy-low.onnx.json

COPY server.py .

ENV PIPER_MODEL=models/en_US-amy-low.onnx \
    PIPER_LENGTH_SCALE=1.0 \
    PIPER_NOISE_SCALE=0.667 \
    PIPER_NOISE_W=0.8 \
    PORT=8001

EXPOSE 8001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]