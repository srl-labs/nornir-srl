FROM python:3.10-slim-buster

# install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app
COPY . .

RUN uv pip install .

ENTRYPOINT ["fcli"]
