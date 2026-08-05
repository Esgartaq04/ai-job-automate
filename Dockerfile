FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --no-cache-dir -e ".[dev]"

# Artifacts land here in local mode; mount a volume or switch to gs:// in prod.
RUN mkdir -p /var/lib/autoapply/artifacts

EXPOSE 8000
CMD ["uvicorn", "autoapply.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
