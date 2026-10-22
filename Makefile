# vboxfront — build orchestration
#
# Thin wrapper over scripts/*.sh. Every target shells out to the matching
# script so the Makefile and scripts never drift.
#
#   make              # standalone binary -> dist/vboxfront
#   make deb           # .deb for the host arch -> dist/vboxfront_<ver>_<arch>.deb
#   make run           # run from source in a local .venv
#   make test          # unit suite (offscreen Qt, no display)
#   make e2e           # unit + end-to-end suite in Docker (no VirtualBox)
#   make clean         # remove build artifacts and .venv
#   make VERSION=1.2.3 deb   # override the version string
#
# VERSION defaults to `git describe`, falling back to the source's own
# APP_VERSION when the checkout has no tags (or is not a checkout at all).
# Exported so build scripts inherit it.

SHELL    := /usr/bin/env bash
BUILD    := ./scripts/build.sh
DEB      := ./scripts/build-deb.sh
E2E      := ./e2e/run.sh
DIST     := dist
APPVER   := $(shell sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' vboxfront_app/version.py)
VERSION  ?= $(shell git describe --tags --dirty 2>/dev/null || echo $(APPVER))
export VERSION

.DEFAULT_GOAL := build

.PHONY: build deb run test e2e e2e-local clean version help

## build: PyInstaller standalone binary -> dist/vboxfront
build:
	$(BUILD)

## deb: Debian package for the host architecture
deb:
	$(DEB)

## run: run from source (provisions .venv on first use)
run:
	$(BUILD) --run

## test: run the unit test suite (offscreen, no display needed)
test:
	$(BUILD) --test

## e2e: unit + end-to-end suite in Docker (hermetic; no VirtualBox needed)
e2e:
	$(E2E) all

## e2e-local: the end-to-end suite against the host Python, without Docker
e2e-local:
	QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s e2e -t . -v

## clean: remove build/, dist/, .venv and PyInstaller caches
clean:
	rm -rf build $(DIST) .venv __pycache__ *.spec.bak
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "cleaned."

## version: print the resolved version string
version:
	@echo "$(VERSION)"

## help: list available targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
