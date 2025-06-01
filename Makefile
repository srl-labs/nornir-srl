NAME=$(shell basename $(PWD))

DIRS = nornir_srl
VERSION = $(shell grep '^version = ' pyproject.toml | head -1 | cut -d'"' -f2)

.PHONY: docker
docker:
	docker build -t "$(NAME):$(VERSION)" -f Dockerfile .

.PHONY: black
black:
	uv run black --check $(DIRS)

.PHONY: mypy
mypy:
	uv run mypy $(DIRS)

.PHONY: tests
tests: black mypy