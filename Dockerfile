# syntax=docker/dockerfile:1

# ---- Stage 1: build the wacli engine (CGO + FTS5) ----
FROM golang:1-bookworm AS wacli-build
ENV CGO_ENABLED=1 CGO_CFLAGS="-Wno-error=missing-braces"
# Pin a tag in production instead of @latest for reproducible builds.
ARG WACLI_VERSION=latest
RUN go install -tags "sqlite_fts5" github.com/openclaw/wacli/cmd/wacli@${WACLI_VERSION}

# ---- Stage 2: runtime (wacli + Python MCP server) ----
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates ffmpeg bash tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=wacli-build /go/bin/wacli /usr/local/bin/wacli

# Python MCP server deps
RUN pip install --no-cache-dir "mcp[cli]>=1.27.1" "pyjwt[crypto]>=2.10.1" "uvicorn>=0.34.0"

WORKDIR /app/server
COPY server/ ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# wacli store + the MCP HTTP port
ENV WACLI_STORE_DIR=/data
EXPOSE 8081

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
