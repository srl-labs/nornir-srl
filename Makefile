NAME=$(shell basename $(PWD))

DIRS = nornir_srl
VERSION = $(shell poetry version -s)

.PHONY: docker
docker:
	docker build -t "$(NAME):$(VERSION)" -f Dockerfile .

.PHONY: black
black:
	poetry run black --check $(DIRS)

.PHONY: mypy
mypy:
	poetry run mypy $(DIRS)

.PHONY: tests
tests: black mypy

