NAME=$(shell basename $(PWD))

DIRS = nornir_srl

.PHONY: black
black:
	poetry run black --check ${DIRS}

.PHONY: mypy
mypy:
	poetry run mypy ${DIRS}

.PHONY: tests
tests: black mypy

