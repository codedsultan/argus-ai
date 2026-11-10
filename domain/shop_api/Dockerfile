FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY domain ./domain

EXPOSE 8080

CMD ["uvicorn", "domain.shop_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
