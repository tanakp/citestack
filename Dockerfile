FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv==0.12.3 && uv sync --frozen --no-dev
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data /app/.cache \
    && chown -R app:app /app
USER app
ENV PATH="/app/.venv/bin:$PATH" HF_HOME=/app/.cache/huggingface
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz')"
ENTRYPOINT ["citestack"]
CMD ["serve", "--host", "0.0.0.0"]
