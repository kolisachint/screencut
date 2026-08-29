# screencut — see docs/architecture.md and docs/implementation-phases.md.
.PHONY: help install schema types generated check-generated typecheck test fixture check clean

FIXTURE ?= data/fixtures/demo01

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

install:  ## Install the package and its dev dependencies.
	python3 -m pip install -e ".[dev]"

schema:  ## Emit JSON Schema for the spec documents and the LLM fragments (§7.2).
	python3 -m spec.schema

types:  ## Generate the review UI's TypeScript types from those schemas (decision #7).
	python3 -m spec.tsgen

generated: schema types  ## Regenerate everything under schemas/.

check-generated: generated  ## Fail if the committed generated files have drifted.
	@git diff --exit-code -- schemas || \
		{ echo "schemas/ is out of date — commit the regenerated files"; exit 1; }

typecheck:  ## Compile the generated TypeScript (phase 1 exit criterion).
	@test -d node_modules || npm install --silent
	npx tsc --noEmit -p tsconfig.json

test:  ## Run the test suite.
	python3 -m pytest -q

fixture:  ## Generate the synthetic fixture job, source video included.
	python3 -m ingest.fixtures --out $(FIXTURE)

check: test check-generated typecheck  ## Everything CI would run.

clean:
	rm -rf .pytest_cache **/__pycache__ node_modules
