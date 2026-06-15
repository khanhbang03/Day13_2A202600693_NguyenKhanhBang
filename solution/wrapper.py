"""YOUR mitigation + observability layer. The simulator calls mitigate() around the
opaque agent (a REAL LLM) for every request. This is the ONLY place observability can
live -- the agent is silent. Legal moves: retry / cache / route / guardrail / sanitize
/ fallback / session-reset / PROMPT ROUTING, plus your own logging/tracing/metrics.
Illegal: hardcoding answers, importing the agent internals, reading instructor files,
network exfiltration.

  call_next(question, config) -> result   # the only way to reach the black box
  context = {"session_id","turn_index","qid","cache": <shared dict>, "cache_lock": <Lock>}
  result  = {"answer","status","steps","trace","meta":{latency_ms,usage,...}}

PROMPT ROUTING: you can override the agent's system prompt PER REQUEST by setting it in
the config you pass to call_next, e.g.:
    conf = dict(config); conf["system_prompt"] = my_better_prompt
    result = call_next(question, conf)
(Or just edit solution/prompt.txt for a single static prompt used on every request.)
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import sys
import time
import traceback
import unicodedata

from telemetry.cost import cost_from_usage
from telemetry.logger import logger, set_correlation_id
from telemetry.redact import redact


_SYSTEM_PROMPT = None
_BAD_STATUSES = {"loop", "max_steps", "no_action", "wrapper_error"}
_NOTE_RE = re.compile(
    r"(?is)\b(?:ghi\s*chu|ghi\s+chú|note|notes|order\s*note|system|developer)\s*[:：].*"
)
_PATHS_READY = False


def _prepare_import_path():
    global _PATHS_READY
    if _PATHS_READY:
        return
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(root_dir, "vendor-py312")]
    exact = f"Python{sys.version_info.major}{sys.version_info.minor}"
    local_appdata = os.environ.get("LOCALAPPDATA")
    exact_roots = [os.path.join(r"C:\\", exact)]
    if local_appdata:
        exact_roots.insert(0, os.path.join(local_appdata, "Programs", "Python", exact))
    for python_dir in exact_roots:
        candidates.append(os.path.join(python_dir, "Lib"))
        candidates.append(os.path.join(python_dir, "Lib", "site-packages"))
    for root in (r"C:\\",):
        try:
            for name in sorted(os.listdir(root), reverse=True):
                if name.startswith("Python"):
                    candidates.append(os.path.join(root, name, "Lib"))
                    candidates.append(os.path.join(root, name, "Lib", "site-packages"))
        except OSError:
            pass
    if local_appdata:
        python_root = os.path.join(local_appdata, "Programs", "Python")
        try:
            for name in sorted(os.listdir(python_root), reverse=True):
                if name.startswith("Python"):
                    candidates.append(os.path.join(python_root, name, "Lib"))
                    candidates.append(os.path.join(python_root, name, "Lib", "site-packages"))
        except OSError:
            pass
    appdata = os.environ.get("APPDATA")
    if appdata:
        python_root = os.path.join(appdata, "Python")
        try:
            for name in sorted(os.listdir(python_root), reverse=True):
                if name.startswith("Python"):
                    candidates.append(os.path.join(python_root, name, "site-packages"))
        except OSError:
            pass
    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
    _PATHS_READY = True


def _load_prompt():
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        path = os.path.join(os.path.dirname(__file__), "prompt.txt")
        with open(path, encoding="utf-8") as f:
            _SYSTEM_PROMPT = f.read().strip()
    return _SYSTEM_PROMPT


def _strip_accents(text):
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _canonical(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _strip_accents(text).lower()
    return re.sub(r"\s+", " ", text).strip()


def _sanitize_question(question):
    cleaned, count = _NOTE_RE.subn("ghi chu: [removed untrusted note]", str(question or ""))
    return cleaned, count


def _cache_get(cache, lock, key):
    with lock:
        value = cache.get(key)
        return copy.deepcopy(value) if value is not None else None


def _cache_set(cache, lock, key, value):
    with lock:
        cache[key] = copy.deepcopy(value)


def _summarize_trace(trace):
    summary = []
    for step in (trace or [])[:8]:
        if isinstance(step, dict):
            summary.append({
                "action": step.get("action") or step.get("tool") or step.get("name"),
                "error": step.get("error"),
                "observation_type": type(step.get("observation")).__name__ if "observation" in step else None,
            })
    return summary


def _call_config(config):
    conf = dict(config or {})
    conf.update({
        "system_prompt": _load_prompt(),
        "temperature": min(float(conf.get("temperature", 0.1) or 0.1), 0.2),
        "loop_guard": True,
        "normalize_unicode": True,
        "redact_pii": True,
        "tool_budget": min(int(conf.get("tool_budget", 4) or 4), 4),
        "max_steps": min(int(conf.get("max_steps", 6) or 6), 6),
        "max_completion_tokens": min(int(conf.get("max_completion_tokens", 360) or 360), 420),
    })
    return conf


def _log_result(event, qid, session_id, turn_index, result, wall_ms, extra=None):
    result = result or {}
    meta = result.get("meta") or {}
    usage = meta.get("usage") or {}
    answer = result.get("answer")
    _, answer_redactions = redact(answer or "")
    data = {
        "qid": qid,
        "session_id": session_id,
        "turn_index": turn_index,
        "status": result.get("status"),
        "steps": result.get("steps"),
        "wall_ms": wall_ms,
        "latency_ms": meta.get("latency_ms"),
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "tools_used": meta.get("tools_used") or [],
        "usage": usage,
        "cost_usd": cost_from_usage(meta.get("model", ""), usage),
        "error": meta.get("error"),
        "error_message": meta.get("error_message"),
        "error_trace": meta.get("error_trace"),
        "answer_redactions": answer_redactions,
        "trace_summary": _summarize_trace(result.get("trace")),
    }
    if extra:
        data.update(extra)
    logger.log_event(event, data)


def mitigate(call_next, question, config, context):
    overall_start = time.time()
    _prepare_import_path()
    context = context or {}
    qid = context.get("qid", "unknown")
    session_id = context.get("session_id", "unknown")
    turn_index = context.get("turn_index", 0)
    set_correlation_id(f"{session_id}:{turn_index}:{qid}")

    cache = context.get("cache")
    lock = context.get("cache_lock")
    sanitized_question, removed_notes = _sanitize_question(question)
    key = "answer:" + hashlib.sha256(_canonical(sanitized_question).encode("utf-8")).hexdigest()

    if removed_notes:
        logger.log_event("INPUT_SANITIZED", {
            "qid": qid,
            "session_id": session_id,
            "turn_index": turn_index,
            "removed_note_spans": removed_notes,
        })

    if cache is not None and lock is not None:
        cached = _cache_get(cache, lock, key)
        if cached is not None:
            meta = cached.setdefault("meta", {})
            meta["cache_hit"] = True
            logger.log_event("CACHE_HIT", {
                "qid": qid,
                "session_id": session_id,
                "turn_index": turn_index,
                "status": cached.get("status"),
            })
            return cached

    conf = _call_config(config)
    attempts = max(1, int(((config or {}).get("retry") or {}).get("max_attempts", 2) or 2))
    best = None

    for attempt in range(1, min(attempts, 3) + 1):
        start = time.time()
        try:
            result = call_next(sanitized_question, conf)
        except Exception as exc:
            result = {
                "answer": None,
                "status": "wrapper_error",
                "steps": 0,
                "trace": [],
                "meta": {
                    "error": type(exc).__name__,
                    "error_message": str(exc),
                    "error_trace": traceback.format_exc(),
                    "session_id": session_id,
                    "turn_index": turn_index,
                },
            }
        wall_ms = int((time.time() - start) * 1000)
        _log_result("REQUEST_ATTEMPT", qid, session_id, turn_index, result, wall_ms, {"attempt": attempt})

        answer = result.get("answer") if isinstance(result, dict) else None
        status = result.get("status") if isinstance(result, dict) else "wrapper_error"
        if answer:
            redacted_answer, redactions = redact(answer)
            if redactions:
                result = copy.deepcopy(result)
                result["answer"] = redacted_answer

        best = result
        if status not in _BAD_STATUSES and answer:
            break

        conf = dict(conf)
        conf["temperature"] = 0
        conf["context_reset_every"] = 1
        time.sleep(0.05 * attempt)

    if cache is not None and lock is not None and best and best.get("status") == "ok" and best.get("answer"):
        _cache_set(cache, lock, key, best)

    _log_result("REQUEST_COMPLETE", qid, session_id, turn_index, best, 0, {
        "sanitized": bool(removed_notes),
        "cache_stored": bool(best and best.get("status") == "ok" and best.get("answer")),
        "total_wall_ms": int((time.time() - overall_start) * 1000),
    })
    return best
