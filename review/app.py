"""FastAPI over `review.service` (architecture.md §8).

One page per job. The routes are thin on purpose: everything that decides
anything lives in `service.py`, and this file only says what is a 404 and what is
a 409.

Single user, no tenancy, no auth (decision #1). This binds to localhost and is
not meant to be reachable from anywhere else.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from spec.corrections import Corrections, StaleCorrection
from spec.profiles import Encoder

from review import service

STATIC = Path(__file__).resolve().parent / "static"


def create_app(
    db_path: Path | str | None = None, encoder: Encoder | None = None
) -> FastAPI:
    """The app, with the two things a review session has to be told.

    `encoder` because a render is keyed on it (§5.2): review with a different one
    than the job was rendered with and the first correction re-encodes everything
    rather than re-encoding what changed. Passing it here rather than reading it
    off the job is honest about the fact that nothing records it.
    """
    app = FastAPI(title="screencut review", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def page(job_id: str | None = None) -> HTMLResponse:
        return HTMLResponse((STATIC / "index.html").read_text())

    @app.get("/api/jobs")
    def jobs() -> dict:
        return {"jobs": [asdict(summary) for summary in service.list_jobs(db_path)]}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict:
        return _load(job_id).payload()

    @app.post("/api/jobs/{job_id}/corrections")
    def correct(job_id: str, corrections: Corrections = Body(...)) -> dict:
        """Save the corrections and re-render.

        The response says which stages ran and which were served from cache,
        because that is §8's whole claim and a reviewer should be able to watch it
        hold rather than take it on faith.
        """
        try:
            view, run = service.correct(
                job_id, corrections, db_path=db_path, encoder=encoder
            )
        except KeyError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from None
        except StaleCorrection as stale:
            # 409 rather than 400: the correction was well-formed when it was
            # made, and the plan moved under it.
            raise HTTPException(status_code=409, detail=str(stale)) from None
        return {
            **view.payload(),
            "ran": run.ran(),
            "cached": [f"{o.profile}/{o.stage}" for o in run.outcomes if o.cached],
            "degradations": run.degradations,
        }

    @app.post("/api/jobs/{job_id}/decision")
    def decision(job_id: str, decision: str = Body(..., embed=True)) -> dict:
        try:
            return service.decide(job_id, decision, db_path=db_path).payload()
        except KeyError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from None
        except ValueError as bad:
            raise HTTPException(status_code=400, detail=str(bad)) from None

    @app.get("/api/jobs/{job_id}/render/{profile}")
    def render(job_id: str, profile: str) -> FileResponse:
        path = _load(job_id).renders.get(profile)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no render for {profile}")
        return FileResponse(path, media_type="video/mp4")

    def _load(job_id: str) -> service.JobView:
        try:
            return service.load_job(job_id, db_path)
        except KeyError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from None

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(prog="screencut-review")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="SQLite path. Defaults to data/screencut.db.")
    parser.add_argument(
        "--encoder",
        choices=[e.value for e in Encoder],
        default=None,
        help="Match how the job was rendered, or the first correction re-encodes from scratch.",
    )
    args = parser.parse_args(argv)
    encoder = Encoder(args.encoder) if args.encoder else None
    uvicorn.run(create_app(args.db, encoder), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
