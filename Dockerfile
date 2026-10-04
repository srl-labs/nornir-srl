FROM python:3.12-slim-bookworm

RUN apt-get update && \
    apt-get install -y curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# install uv

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.cargo/bin:/root/.local/bin:$PATH"

WORKDIR /app
COPY . .

ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}

# The versions uv.lock pins, not the newest ones: an unpinned install picks
# up whatever was released last, and a scrapli release has broken
# nornir-scrapli on import before.
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt && \
    uv pip install --system -r /tmp/requirements.txt && \
    uv pip install --system --no-deps . && \
    rm /tmp/requirements.txt

# Everything fcli keeps on disk - the history, baselines, configurations,
# snapshots, cabling and acknowledgements - goes under one fixed directory,
# whatever user the container runs as, so a volume mounted there keeps it.
# World-writable and sticky like /tmp: a named volume is created with these
# permissions, so a container run with --user can write to it too.
ENV XDG_STATE_HOME=/state
RUN mkdir -p /state && chmod 1777 /state

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
