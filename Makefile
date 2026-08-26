.PHONY: install portal api console test build deploy clean bench

PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
REGION  ?= us-central1
SERVICE ?= clickwright

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"
	.venv/bin/playwright install --with-deps chromium
	cd frontend && pnpm install

# the target system the agent has to operate
portal:
	PYTHONPATH=. .venv/bin/uvicorn portal.app:app --port 8081 --reload

# the same target, after somebody redesigned it
portal-drift:
	DRIFT=1 PYTHONPATH=. .venv/bin/uvicorn portal.app:app --port 8081 --reload

api:
	PYTHONPATH=. .venv/bin/uvicorn app.server:app --port 8080 --reload --env-file .env

console:
	cd frontend && pnpm dev

test:
	PYTHONPATH=. .venv/bin/python -m pytest tests -q

bench:
	PYTHONPATH=. .venv/bin/python -m bench.measure --connector vendor-portal

build:
	cd frontend && pnpm build

deploy: build
	gcloud run deploy $(SERVICE) \
		--source . \
		--project $(PROJECT) \
		--region $(REGION) \
		--cpu 2 --memory 4Gi --cpu-boost --timeout 3600 \
		--set-env-vars GOOGLE_CLOUD_PROJECT=$(PROJECT),GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_LOCATION=$(REGION) \
		--allow-unauthenticated

clean:
	rm -rf var frontend/dist .pytest_cache
