FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python ingest.py && useradd -m -u 10002 rag && chown -R rag /app
USER rag
EXPOSE 8200
CMD ["uvicorn", "rag_service:app", "--host", "0.0.0.0", "--port", "8200"]
