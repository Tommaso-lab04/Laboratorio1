FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libmariadb3 libmariadb-dev \
        python3-dev pkg-config build-essential && \
    rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN crawl4ai-setup || true
COPY backend/ /app/
COPY domains.json /app/domains.json
COPY gs_data /app/gs_data
EXPOSE 8003
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8003"]
