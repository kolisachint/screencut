# screencut — see docs/architecture.md and docs/implementation-phases.md.
.PHONY: help install schema types generated check-generated typecheck test fixture run take review broken check clean

FIXTURE ?= data/fixtures/demo01
CAP ?= data/fixtures/take01.cap
JOB ?= data/jobs/take01
ENCODER ?= software

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

install:  ## Install the package and its dev dependencies.
	python3 -m pip install -e ".[dev]"

schema:  ## Emit JSON Schema for the spec documents and the LLM fragments (§7.2).
	python3 -m spec.schema

types:  ## Generate the review UI's TypeScript types from those schemas (decision #7).
	python3 -m spec.tsgen

generated: schema types  ## Regenerate everything under schemas/.

check-generated: generated  ## Fail unless the *committed* generated files match the models.
	@git diff HEAD --exit-code -- schemas > /dev/null || \
		{ echo "schemas/ differs from HEAD — commit the regenerated files"; exit 1; }

typecheck:  ## Compile the generated TypeScript (phase 1 exit criterion).
	@test -d node_modules || npm install --silent
	npx tsc --noEmit -p tsconfig.json

test:  ## Run the test suite.
	python3 -m pytest -q

fixture:  ## Generate the synthetic fixture job, source video included.
	python3 -m ingest.fixtures --out $(FIXTURE)

run: fixture  ## Run the fixture job through the pipeline. ENCODER=videotoolbox on macOS.
	python3 -m runner.cli run $(FIXTURE) --encoder $(ENCODER)

take:  ## Cap-format take -> job -> both renders. Needs whisper-cli + weights (see AGENTS.md).
	python3 -m ingest.cap_fixture --out $(CAP)
	python3 -m runner.cli ingest $(CAP) --out $(JOB)
	python3 -m runner.cli run $(JOB) --encoder $(ENCODER)

review:  ## Serve the review UI (§8). ENCODER must match how the jobs were rendered.
	python3 -m review.app --encoder $(ENCODER)

broken:  ## Run the deliberately bad fixture, so §9.1's checks are seen firing.
	python3 -m ingest.fixtures --out data/fixtures/broken01 --job-id fixture --broken
	-python3 -m runner.cli run data/fixtures/broken01 --encoder $(ENCODER)

check: test check-generated typecheck  ## Everything CI would run.

clean:
	rm -rf .pytest_cache **/__pycache__ node_modules
