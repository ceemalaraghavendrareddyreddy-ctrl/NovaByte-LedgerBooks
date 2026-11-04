FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TRUST_PROXY=1

WORKDIR /app

COPY requirements.txt requirements-gcp.txt ./
RUN pip install --no-cache-dir -r requirements-gcp.txt

COPY . .

# Cloud Run sets PORT (8080); serve.py reads it.
CMD ["python", "serve.py"]
