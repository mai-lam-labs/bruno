# The relay's Python server (python -m bruno.server), published as ghcr.io/mai-lam-labs/bruno,
# with the default configuration (config.default.json) built in. The float's key, and the
# payee, are given when it runs (DEPLOYMENT.md, Docker).

# uv, the version the Makefile pins
FROM ghcr.io/astral-sh/uv:0.12.16 AS uv

FROM python:3.14-slim AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# the locked dependencies first, a layer that changes only with uv.lock
COPY pyproject.toml uv.lock README.md LICENSE-MIT LICENSE-APACHE ./
RUN uv sync --locked --no-dev --no-install-project
# then the relay itself, installed into the environment, its tests left out
COPY bruno ./bruno
RUN rm -rf bruno/tests && uv sync --locked --no-dev --no-editable

FROM python:3.14-slim
RUN useradd --system --uid 10001 --no-create-home bruno
COPY --from=build /app/.venv /app/.venv
COPY config.default.json /etc/bruno/config.json
# its defaults, each overridden by an environment variable or an option (DEPLOYMENT.md, Docker)
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    BRUNO_HOST=0.0.0.0 \
    BRUNO_CONFIG=/etc/bruno/config.json
USER bruno
EXPOSE 8402
ENTRYPOINT ["python", "-m", "bruno.server"]
