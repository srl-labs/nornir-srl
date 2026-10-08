NAME=$(shell basename $(PWD))

DIRS = nornir_srl

# The version setuptools-scm derives from git, as the package itself is built
# with: pyproject.toml has no static one. Resolved on first use only, so
# targets that do not need it do not pay for it; override with VERSION=...
VERSION = $(eval VERSION := $(shell uvx --quiet --from 'setuptools-scm>=8.4.2' setuptools-scm 2>/dev/null))$(VERSION)
# A Docker tag cannot hold the '+' of a local version (0.9.1.dev5+gf59fc7f0c).
TAG = $(subst +,-,$(VERSION))

.PHONY: docker
docker:
	@test -n "$(VERSION)" || { echo "could not resolve the version with setuptools-scm; pass VERSION=..." >&2; exit 1; }
	docker build --build-arg VERSION="$(VERSION)" -t "$(NAME):$(TAG)" -f Dockerfile .

.PHONY: tests
tests:
	uv run --extra dev pytest tests
