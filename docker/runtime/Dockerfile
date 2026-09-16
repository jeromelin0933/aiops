FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src
COPY scripts/run_correlation_runtime.py /app/scripts/run_correlation_runtime.py
COPY configs/runtime_orchestration.docker.yaml /app/configs/runtime_orchestration.docker.yaml

CMD ["python", "scripts/run_correlation_runtime.py", "--config", "configs/runtime_orchestration.docker.yaml"]
