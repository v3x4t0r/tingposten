"""Model backends: the subscription CLIs (`claude -p`, `codex exec`), no API keys.

Every call gets the whole prompt on stdin, a strict JSON schema and no
tools: the models read only what the prompt holds, run in an empty temp
directory, and cannot touch the repo or the network on our behalf. The
answer is validated against the schema here; a malformed answer gets one
retry with the error, then LLMError. Steps share the plumbing at the bottom
(atomic JSON writes, a small thread pool).
"""

import json
import logging
import os
import subprocess
import tempfile
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

CLAUDE_TIMEOUT_S = 900
CODEX_TIMEOUT_S = 1800
# Runaway guard per Claude call; the subscription reports a notional cost.
CLAUDE_MAX_BUDGET_USD = "3"


class LLMError(RuntimeError):
    pass


class _Backend:
    name = "?"

    def ask(self, prompt: str, schema: dict) -> dict:
        try:
            return validate(self._call(prompt, schema), schema)
        except LLMError as first:
            log.warning("%s: malformed answer (%s), retrying once", self.name, first)
            retry = (f"{prompt}\n\nDitt forrige svar var ugyldig: {first}. "
                     "Svar bare med JSON som følger skjemaet nøyaktig.")
            return validate(self._call(retry, schema), schema)

    def _call(self, prompt: str, schema: dict) -> dict:
        raise NotImplementedError


class ClaudeCLI(_Backend):
    def __init__(self, model: str):
        self.model = model
        self.name = f"claude:{model}"

    def _call(self, prompt: str, schema: dict) -> dict:
        argv = [
            "claude", "-p", "--model", self.model,
            "--output-format", "json", "--json-schema", json.dumps(schema),
            # No tools, no user/project settings, no MCP servers, no saved session.
            "--tools", "", "--restricted", "--strict-mcp-config", "--no-session-persistence",
            "--max-budget-usd", CLAUDE_MAX_BUDGET_USD,
        ]
        with tempfile.TemporaryDirectory(prefix="tingposten-claude-") as tmp:
            done = _run(argv, prompt, CLAUDE_TIMEOUT_S, cwd=tmp)
        try:
            out = json.loads(done.stdout)
        except ValueError:
            raise LLMError(f"claude gave no JSON (exit {done.returncode}): {_tail(done)}") from None
        if out.get("is_error") or out.get("subtype") != "success":
            raise LLMError(f"claude failed ({out.get('subtype')}): {str(out.get('result'))[-500:]}")
        data = out.get("structured_output")
        if data is None:
            try:
                data = json.loads(out.get("result") or "")
            except ValueError:
                raise LLMError("claude answer is not JSON") from None
        return data


class CodexCLI(_Backend):
    def __init__(self, model: str | None = None):
        self.model = model
        self.name = f"codex:{model or _codex_default_model() or 'standard'}"

    def _call(self, prompt: str, schema: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix="tingposten-codex-") as tmp:
            schema_file, answer = Path(tmp) / "skjema.json", Path(tmp) / "svar.json"
            schema_file.write_text(json.dumps(schema))
            argv = [
                "codex", "exec", "--skip-git-repo-check", "--ephemeral",
                # Most restrictive: read-only sandbox, never ask, empty working dir.
                "-s", "read-only", "-c", 'approval_policy="never"', "-C", tmp,
                "--output-schema", str(schema_file), "-o", str(answer), "--color", "never",
            ]
            if self.model:
                argv += ["-m", self.model]
            done = _run([*argv, "-"], prompt, CODEX_TIMEOUT_S, cwd=tmp)
            if done.returncode or not answer.exists():
                raise LLMError(f"codex failed (exit {done.returncode}): {_tail(done)}")
            try:
                return json.loads(answer.read_text())
            except ValueError:
                raise LLMError("codex answer is not JSON") from None


class MedReserve(_Backend):
    """Asks `forste`; when it can't answer (not installed, out of quota, timed out,
    an answer that stays malformed), asks `reserve` and keeps to it for the rest
    of the run, so a missing CLI isn't tried again for every sak. `name` is the
    backend that gave this thread's last answer, which is what the steps record."""

    def __init__(self, forste: _Backend, reserve: _Backend | None):
        self.forste, self.reserve = forste, reserve
        self.byttet = False
        self._lock = threading.Lock()
        self._svarte = threading.local()

    @property
    def name(self) -> str:
        brukt = getattr(self._svarte, "backend", None) or (self.reserve if self.byttet else self.forste)
        return brukt.name

    def ask(self, prompt: str, schema: dict) -> dict:
        if not self.byttet or self.reserve is None:
            try:
                svar = self.forste.ask(prompt, schema)
                self._svarte.backend = self.forste
                return svar
            except LLMError as e:
                if self.reserve is None:
                    raise
                with self._lock:
                    if not self.byttet:
                        log.warning("%s can't answer (%s); %s takes over for the rest of this run",
                                    self.forste.name, str(e)[:200], self.reserve.name)
                        self.byttet = True
        svar = self.reserve.ask(prompt, schema)
        self._svarte.backend = self.reserve
        return svar


class FakeLLM(_Backend):
    """For tests: returns the given responses in order (an Exception is raised)."""

    def __init__(self, responses, name: str = "fake"):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.name = name

    def _call(self, prompt: str, schema: dict) -> dict:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("FakeLLM ran out of responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _run(argv: list[str], prompt: str, timeout: int, cwd: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise LLMError(f"{argv[0]} timed out after {timeout}s") from None
    except FileNotFoundError:
        raise LLMError(f"{argv[0]} is not installed") from None


def _tail(done: subprocess.CompletedProcess) -> str:
    return ((done.stdout or "") + (done.stderr or ""))[-800:]


def _codex_default_model() -> str | None:
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        return tomllib.loads(path.read_text()).get("model")
    except (OSError, tomllib.TOMLDecodeError):
        return None


# --- schemas -------------------------------------------------------------------

def obj(**props) -> dict:
    """A strict object schema: every property required, nothing extra (Codex needs that)."""
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def validate(data, schema: dict, path: str = "$"):
    """Check `data` against the small JSON Schema subset our schemas use."""
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                return validate(data, option, path)
            except LLMError as e:
                errors.append(str(e))
        raise LLMError(f"{path}: matches no allowed shape ({'; '.join(errors)})")
    kind = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: type(v) is int,
        "boolean": lambda v: type(v) is bool,
        "null": lambda v: v is None,
    }
    if kind and not checks[kind](data):
        raise LLMError(f"{path}: expected {kind}, got {type(data).__name__}")
    if "enum" in schema and data not in schema["enum"]:
        raise LLMError(f"{path}: {data!r} not in {schema['enum']}")
    if kind == "integer":
        if "minimum" in schema and data < schema["minimum"] or "maximum" in schema and data > schema["maximum"]:
            raise LLMError(f"{path}: {data} out of range")
    if kind == "object":
        props = schema.get("properties", {})
        missing = [k for k in schema.get("required", []) if k not in data]
        if missing:
            raise LLMError(f"{path}: missing {missing}")
        if schema.get("additionalProperties") is False and (extra := set(data) - set(props)):
            raise LLMError(f"{path}: unexpected {sorted(extra)}")
        for k, v in data.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    if kind == "array":
        for i, v in enumerate(data):
            validate(v, schema.get("items", {}), f"{path}[{i}]")
    return data


def fence(name: str, text: str) -> str:
    """Untrusted document text between markers it cannot close itself."""
    safe = text.replace("<<<", "‹‹‹").replace(">>>", "›››")
    return f"<<<{name}\n{safe}\n{name}>>>"


# --- step plumbing --------------------------------------------------------------

def read_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def run_parallel(fn, items: list[dict], parallel: int = 3) -> dict:
    """fn(item) -> short status string; returns {status: count} and the failures."""
    counts: dict[str, int] = {}
    failures: list[str] = []

    def one(item):
        try:
            return fn(item)
        except Exception as e:  # one bad sak must not stop the others
            log.exception("step failed for %s", item.get("referansenr") or item.get("id"))
            return f"feil: {e}"

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for item, status in zip(items, pool.map(one, items)):
            key = status.split(":", 1)[0]
            counts[key] = counts.get(key, 0) + 1
            if key in ("feil", "blocked"):
                failures.append(f"{item.get('referansenr') or item.get('id')}: {status}")
    return {"antall": counts, "feil": failures}
