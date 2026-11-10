FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-agent.txt .
RUN pip install --no-cache-dir -r requirements-agent.txt

COPY domain ./domain
COPY services ./services
COPY evals ./evals

EXPOSE 8010

CMD ["python", "-m", "services.agent.main"]
