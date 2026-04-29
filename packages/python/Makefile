.PHONY: install dev lint format test build clean

## Install the SDK in the current environment
install:
	pip install .

## Install with development dependencies
dev:
	pip install -e ".[dev]"

## Run linter (ruff check)
lint:
	ruff check src/ tests/

## Auto-format code
format:
	ruff format src/ tests/

## Run the test suite
test:
	pytest tests/ -v

## Run tests with coverage
coverage:
	pytest tests/ -v --cov=chipzen --cov-report=term-missing

## Build distribution packages
build:
	python -m build

## Remove build artifacts
clean:
	rm -rf dist/ build/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

## Validate that the scaffolded bot template passes checks
validate-template:
	@tmpdir=$$(mktemp -d) && \
	chipzen-sdk init test_bot --dir "$$tmpdir" && \
	chipzen-sdk validate "$$tmpdir/test_bot" && \
	rm -rf "$$tmpdir"

## Show help
help:
	@echo "Available targets:"
	@echo "  install           Install the SDK"
	@echo "  dev               Install with dev dependencies"
	@echo "  lint              Run ruff linter"
	@echo "  format            Auto-format code"
	@echo "  test              Run test suite"
	@echo "  coverage          Run tests with coverage"
	@echo "  build             Build distribution packages"
	@echo "  clean             Remove build artifacts"
	@echo "  validate-template Validate the scaffolded bot template"
