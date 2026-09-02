"""The review UI (architecture.md §8, decision #4).

Corrections are captured as structural diffs against the spec, because that is
what makes §10 possible: "the reviewer reinstated a 0.7s silence" is a row a
learner can average over, and "the reviewer moved a slider" is not.

`service.py` is the loop with no HTTP in it; `app.py` is the FastAPI surface over
it; `static/` is the page. Importing `app` needs FastAPI installed
(`pip install -e ".[review]"`), which is why it is not imported here.
"""

from review.service import JobSummary, JobView, correct, decide, list_jobs, load_job

__all__ = ["JobSummary", "JobView", "correct", "decide", "list_jobs", "load_job"]
