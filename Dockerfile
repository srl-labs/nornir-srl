FROM python:3.10-buster AS builder

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache \
    POETRY_VERSION=1.5.0 \
    PYTHON_PKG="nornir-srl"

RUN pip install "poetry==$POETRY_VERSION"

WORKDIR /app

COPY poetry.lock pyproject.toml ./
COPY . ./
RUN poetry install --no-dev 

FROM python:3.10-slim-buster as runtime

ENV VIRTUAL_ENV=/app/.venv \
      PATH="/app/.venv/bin:$PATH"

COPY --from=builder ${VIRTUAL_ENV} ${VIRTUAL_ENV}
COPY ./${PYTHON_PKG} /app/${PYTHON_PKG}

ENTRYPOINT [ "fcli" ]


