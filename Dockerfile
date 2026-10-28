FROM python:3.11-slim

# FFmpeg (con libass) y fuentes para los subtítulos
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fontconfig fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Descarga el modelo de Whisper al construir, para que el primer video no espere
ARG WHISPER_MODEL=small
ENV WHISPER_MODEL=${WHISPER_MODEL}
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY . .
ENV PORT=8000 DATA_DIR=/data
RUN mkdir -p /data
EXPOSE 8000

# 1 worker + hilos: el render usa un lock, así que se procesa un video a la vez
CMD gunicorn -w 1 --threads 8 --timeout 0 -b 0.0.0.0:${PORT} app_web:app
