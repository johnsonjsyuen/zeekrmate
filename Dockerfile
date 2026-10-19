FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fordlogger/ fordlogger/
COPY scripts/ scripts/
COPY sql/ sql/

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "from fordlogger.config import load_config; from fordlogger import db; db.connect(load_config('config.json')).close()" || exit 1

CMD ["python", "-m", "fordlogger"]
