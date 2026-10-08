# Makefile for AMRrules

.PHONY: dev build clean web serve-web

# Copy rules and install in editable mode
dev:
	@echo "🔁 Copying rule files..."
	python copy_rules.py
	@echo "📦 Installing package in editable mode..."
	pip install -e .

# Build distribution packages (wheel + sdist)
build:
	@echo "🔁 Copying rule files for build..."
	python copy_rules.py
	@echo "🚀 Building package..."
	python -m build

# Clean generated artifacts
clean:
	@echo "🧹 Cleaning build artifacts..."
	rm -rf build dist *.egg-info
# Build static assets for the browser UI (web/build/)
web:
	python scripts/build_web.py

# Preview the browser UI locally
serve-web:
	python -m http.server -d web 8000
