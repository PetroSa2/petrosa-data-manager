.PHONY: help setup install install-dev clean lint format test security build run run-docker deploy k8s-status k8s-logs k8s-clean pipeline docker-clean

# Default target
.DEFAULT_GOAL := help

# Variables
PYTHON := python3
PIP := $(PYTHON) -m pip
VENV := .venv
KUBECONFIG := k8s/kubeconfig.yaml
NAMESPACE := petrosa-apps
APP_NAME := petrosa-data-manager
DOCKER_IMAGE := $(APP_NAME):latest
DOCKER_REGISTRY := your-registry

help: ## Show this help message
	@echo "Petrosa Data Manager - Makefile Commands"
	@echo "=========================================="
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Complete environment setup
	@echo "Setting up development environment..."
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip setuptools wheel
	$(VENV)/bin/pip install -r requirements-dev.txt
	@echo "✅ Setup complete! Activate with: source $(VENV)/bin/activate"

install: ## Install production dependencies
	$(PIP) install -r requirements.txt

install-dev: ## Install development dependencies
	$(PIP) install -r requirements-dev.txt

clean: ## Clean up generated files
	@echo "Cleaning up..."
	rm -rf $(VENV)
	rm -rf __pycache__
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	rm -rf htmlcov
	rm -rf .coverage
	rm -rf coverage.xml
	rm -rf dist
	rm -rf build
	rm -rf *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "✅ Cleanup complete!"

lint: ## Run all linters
	@echo "Running linters..."
	$(VENV)/bin/flake8 data_manager tests --max-line-length=100
	$(VENV)/bin/black --check data_manager tests
	$(VENV)/bin/ruff check data_manager tests
	$(VENV)/bin/mypy data_manager
	@echo "✅ Linting complete!"

format: ## Format code
	@echo "Formatting code..."
	$(VENV)/bin/black data_manager tests
	$(VENV)/bin/ruff check --fix data_manager tests
	@echo "✅ Formatting complete!"

test: ## Run tests
	@echo "Running tests..."
	$(VENV)/bin/pytest tests/ -v --cov=data_manager --cov-report=term --cov-report=html
	@echo "✅ Tests complete!"

security: ## Run security scan
	@echo "Running security scan..."
	$(VENV)/bin/bandit -r data_manager -f json -o bandit-report.json || true
	@echo "✅ Security scan complete!"

build: ## Build Docker image
	@echo "Building Docker image..."
	docker build -t $(DOCKER_IMAGE) .
	@echo "✅ Build complete!"

run: ## Run locally
	@echo "Running Data Manager locally..."
	$(VENV)/bin/python -m data_manager.main

run-docker: ## Run in Docker
	@echo "Running Data Manager in Docker..."
	docker run --rm \
		--name $(APP_NAME) \
		-p 8000:8000 \
		-e NATS_URL=${NATS_URL} \
		-e POSTGRES_URL=${POSTGRES_URL} \
		-e MONGODB_URL=${MONGODB_URL} \
		$(DOCKER_IMAGE)

deploy: ## Deploy to Kubernetes
	@echo "Deploying to Kubernetes..."
	@if [ ! -f $(KUBECONFIG) ]; then \
		echo "❌ kubeconfig not found at $(KUBECONFIG)"; \
		exit 1; \
	fi
	kubectl --kubeconfig=$(KUBECONFIG) apply -f k8s/
	@echo "✅ Deployment complete!"

k8s-status: ## Check Kubernetes deployment status
	@echo "Checking deployment status..."
	kubectl --kubeconfig=$(KUBECONFIG) -n $(NAMESPACE) get all -l app=$(APP_NAME)
	kubectl --kubeconfig=$(KUBECONFIG) -n $(NAMESPACE) get pods -l app=$(APP_NAME)

k8s-logs: ## View Kubernetes logs
	@echo "Fetching logs..."
	kubectl --kubeconfig=$(KUBECONFIG) -n $(NAMESPACE) logs -l app=$(APP_NAME) --tail=100 -f

k8s-clean: ## Clean up Kubernetes resources
	@echo "Cleaning up Kubernetes resources..."
	kubectl --kubeconfig=$(KUBECONFIG) -n $(NAMESPACE) delete -f k8s/ || true
	@echo "✅ Cleanup complete!"

pipeline: ## Run complete local pipeline
	@echo "Running complete pipeline..."
	$(MAKE) clean
	$(MAKE) setup
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) security
	$(MAKE) build
	@echo "✅ Pipeline complete!"

docker-clean: ## Clean Docker images
	@echo "Cleaning Docker images..."
	docker rmi $(DOCKER_IMAGE) || true
	docker system prune -f
	@echo "✅ Docker cleanup complete!"

