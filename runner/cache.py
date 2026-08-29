"""Content-addressed cache keys (architecture.md §5.2).

Keyed on `(stage_name, stage_version, input_hash, params_hash)`. Non-negotiable —
principle 4 — and the one subtlety that will not announce itself is in
`params_hash`: for a model stage it **must** include the model identifier and a
prompt version. The same transcript under a different model, or under a revised
prompt, is a different artifact, and a key that omits them serves a stale result
after exactly the change you were trying to evaluate. It looks like the prompt
edit had no effect.

No model stage exists yet. The key is built correctly anyway, and
`require_model_params` refuses to compute one without them, because the bug it
prevents is silent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CHUNK = 1 << 20


def canonical(payload: Any) -> str:
    """One byte string per value, so a key never depends on dict ordering."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(payload: Any) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    """Hash a media file.

    The full contents, not size-and-mtime: a re-encoded take with the same length
    must not serve the old render, and that is a silently wrong output rather than
    a crash. It costs a pass over the source once per run, which is worth
    revisiting when a real take makes the number visible (phase 4).
    """
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


class MissingModelParams(ValueError):
    pass


def require_model_params(stage: str, params: dict[str, Any]) -> None:
    missing = [key for key in ("model", "prompt_version") if key not in params]
    if missing:
        raise MissingModelParams(
            f"model stage {stage!r} is missing {missing} from its params. Without them the "
            f"cache serves the old answer after a model or prompt change, and it looks "
            f"like the change had no effect (§5.2)."
        )


def cache_key(
    *,
    stage: str,
    stage_version: int,
    inputs: Any,
    params: dict[str, Any],
    model_backed: bool = False,
) -> str:
    if model_backed:
        require_model_params(stage, params)
    return digest(
        {
            "stage": stage,
            "stage_version": stage_version,
            "input_hash": digest(inputs),
            "params_hash": digest(params),
        }
    )
