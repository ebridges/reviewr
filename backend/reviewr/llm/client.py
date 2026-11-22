"""One helper for structured AI calls, shared by every AI feature. It goes to Claude or
Codex depending on the provider chosen in the app (see ai.py)."""

from __future__ import annotations

import json
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from reviewr import ai, secrets
from reviewr.config import LLMConfig, load_config
from reviewr.guidance import with_guidance

T = TypeVar("T", bound=BaseModel)

CODEX_TIMEOUT_S = 900


class LLMError(RuntimeError):
    pass


def structured(cfg: LLMConfig, system: str, user: str, schema: type[T], max_tokens: int = 32000,
               *, step: str, slug: str | None = None) -> T:
    """Ask the current provider for output matching `schema`. `step` and `slug` pick the
    guidance added to the system prompt (see guidance.py)."""
    system = with_guidance(system, step, slug)
    if ai.current() == "codex":
        return _codex(system, user, schema)
    return _claude(cfg, system, user, schema, max_tokens)


# Claude

def anthropic_client() -> anthropic.Anthropic:
    """A client signed in the way Settings says: with the `ant auth login` profile (or
    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN from the environment, which the SDK
    prefers), or with the API key saved in the Keychain."""
    if load_config().ai.claude_auth == "api_key":
        key = secrets.get_secret(secrets.ANTHROPIC_API_KEY)
        if not key:
            raise LLMError("No Anthropic API key is saved. Add one under Settings → AI, or sign in with the ant CLI instead.")
        return _client_for(key)
    return _client_for(None)


@lru_cache(maxsize=4)
def _client_for(api_key: str | None) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def _claude(cfg: LLMConfig, system: str, user: str, schema: type[T], max_tokens: int) -> T:
    """Streamed because the SDK refuses non-streaming requests with a large max_tokens.
    If a safety classifier declines, the API retries on a fallback model inside the
    same call instead of returning a refusal."""
    with anthropic_client().beta.messages.stream(
        model=cfg.model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
        output_config={"effort": cfg.effort},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise LLMError(f"Claude returned no usable result (stop_reason={response.stop_reason})")
    return response.parsed_output


# Codex

def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI structured outputs require every object to list all its properties as
    required and to forbid extra ones; optional fields are already `anyOf [..., null]`."""

    def fix(node: Any) -> Any:
        if isinstance(node, list):
            return [fix(v) for v in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in ("default", "title"):
                continue  # schema keywords, not data
            if k in ("properties", "$defs"):
                # Maps of names to schemas: keep every name (a field may be called "title").
                out[k] = {name: fix(sub) for name, sub in v.items()}
            else:
                out[k] = fix(v)
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return fix(schema)


def _codex(system: str, user: str, schema: type[T]) -> T:
    """Run `codex exec` read-only in an empty folder, with the answer constrained to the
    schema. Uses the `codex login` session."""
    with tempfile.TemporaryDirectory(prefix="reviewr-codex-") as tmp:
        work = Path(tmp)
        (work / "schema.json").write_text(json.dumps(strict_schema(schema.model_json_schema())))
        argv = [
            "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
            "-s", "read-only", "-C", str(work),
            "--output-schema", str(work / "schema.json"), "-o", str(work / "out.json"),
        ]
        if model := load_config().ai.codex_model:
            argv += ["-m", model]
        prompt = f"{system}\n\n{user}\n\nAnswer with the JSON object only. Don't run commands or read files; everything you need is above."
        try:
            proc = subprocess.run(argv + ["-"], input=prompt, capture_output=True, text=True, timeout=CODEX_TIMEOUT_S, check=False)
        except FileNotFoundError as e:
            raise LLMError("The Codex CLI isn't installed. Install it, run `codex login`, or switch to Claude.") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"Codex didn't answer within {CODEX_TIMEOUT_S // 60} minutes.") from e
        out = work / "out.json"
        if proc.returncode < 0:
            raise LLMError(f"Codex was stopped before it answered (signal {-proc.returncode}).")
        if proc.returncode != 0 or not out.exists():
            # Codex echoes the prompt to stderr; only what follows it is the error.
            err = (proc.stderr or proc.stdout).split("Don't run commands or read files; everything you need is above.")[-1]
            tail = err.strip()[-600:]
            raise LLMError(f"Codex failed (exit {proc.returncode}): {tail}")
        try:
            return schema.model_validate_json(out.read_text())
        except ValidationError as e:
            raise LLMError(f"Codex's answer didn't match the expected shape: {e.errors()[:2]}") from e
