# syntax=docker/dockerfile:1.7

FROM python:3.14-slim AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-agent.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements-agent.txt


FROM python:3.14-slim

ARG TARGETARCH
ARG ARGUS_VERSION=dev
ARG OTELCOL_VERSION=0.159.0
ARG JAEGER_VERSION=1.62.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        supervisor \
        tar \
    && rm -rf /var/lib/apt/lists/*

# Collector and Jaeger run as local processes in this single-container
# deployment. The proxy is the only process bound to the public port.
RUN curl -fsSL \
      "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTELCOL_VERSION}/otelcol-contrib_${OTELCOL_VERSION}_linux_${TARGETARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin otelcol-contrib \
    && curl -fsSL \
      "https://github.com/jaegertracing/jaeger/releases/download/v${JAEGER_VERSION}/jaeger-${JAEGER_VERSION}-linux-${TARGETARCH}.tar.gz" \
      | tar -xz -C /tmp \
    && mv "/tmp/jaeger-${JAEGER_VERSION}-linux-${TARGETARCH}/jaeger-all-in-one" /usr/local/bin/ \
    && rm -rf /tmp/jaeger-*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY domain ./domain
COPY services ./services
COPY evals ./evals
COPY deploy/public ./deploy/public

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    ARGUS_VERSION=${ARGUS_VERSION} \
    PUBLIC_BFF_TOKEN="" \
    PUBLIC_RATE_LIMIT_PER_MINUTE="12" \
    PUBLIC_MAX_CONCURRENT="4" \
    MAX_PROMPT_BYTES="16384"

EXPOSE 8080

CMD ["supervisord", "-c", "/app/deploy/public/supervisord.conf"]
