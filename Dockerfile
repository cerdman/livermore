# Python 3.14 reads Codex's compressed .jsonl.zst sessions without extra packages.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && rm -rf /app/src

RUN useradd --create-home --uid 10001 livermore \
    && mkdir -p /data && chown livermore:livermore /data
USER livermore
WORKDIR /data
VOLUME ["/data"]

ENV LIVERMORE_DB=/data/observatory.sqlite3
EXPOSE 4318

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4318/healthz', timeout=2)"]

ENTRYPOINT ["observatory"]
CMD ["serve", "--host", "0.0.0.0", "--port", "4318"]
