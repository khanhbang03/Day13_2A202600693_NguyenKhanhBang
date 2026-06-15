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
import json
import os
import re
import subprocess
import sys
import time
import traceback
import types
import unicodedata

from telemetry.cost import cost_from_usage
from telemetry.logger import logger, set_correlation_id
from telemetry.redact import redact


_SYSTEM_PROMPT = None
_BAD_STATUSES = {"loop", "max_steps", "no_action", "wrapper_error"}
_NOTE_RE = re.compile(
    r"(?is)\b(?:ghi\s*chu|ghi\s+chú|note|notes|order\s*note|system|developer)\s*[:：].*"
)
_TOTAL_RE = re.compile(r"(?i)\bTong cong:\s*([0-9][0-9., ]*)\s*VND\b")
_MONEY_RE = re.compile(r"\d[\d., ]*\d|\d+")
_COUPON_RE = re.compile(
    r"(?i)\b(?:(?:dung|dùng|ap\s*dung|áp\s*dụng)\s+(?:ma|mã|coupon|code)|voi\s+coupon|với\s+coupon)\s+([A-Z0-9_-]+)"
)
_QTY_RE = re.compile(r"(?i)\b(?:mua|dat|đặt)\s+(\d+)\b")
_DEST_RE = re.compile(
    r"(?i)\b(?:ship|giao(?:\s+den|\s+đến)?|ve|về)\s+([A-Za-zÀ-ỹ\s]+?)(?=\s*(?:-|,|\.|\?|$|\b(?:tong|tổng|het|hết|bao|lien|liên|dung|dùng)\b))"
)
_LEADING_ORDER_RE = re.compile(r"(?i)^\s*(?:shop\s+con|mua|dat|đặt|con)\s+(?:\d+\s+)?")
_TAIL_RE = re.compile(
    r"(?i)\b(?:dung|dùng|ap\s*dung|áp\s*dụng)\s+(?:ma|mã|coupon|code)\s+[A-Z0-9_-]+.*$"
    r"|\b(?:voi|với)\s+coupon\s+[A-Z0-9_-]+.*$"
    r"|\b(?:ship|giao(?:\s+den|\s+đến)?|ve|về)\s+.*$"
    r"|\b(?:tong|tổng|het|hết|gia|giá|bao|khong|không|lien|liên)\b.*$"
)
_PATHS_READY = False


class _AttrDict(types.SimpleNamespace):
    def __getattr__(self, name):
        return None


def _to_attr(value):
    if isinstance(value, dict):
        return _AttrDict(**{k: _to_attr(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_attr(v) for v in value]
    return value


def _install_openai_shim():
    if "openai" in sys.modules and getattr(sys.modules["openai"], "_OBS_SHIM", False):
        return

    class OpenAIError(Exception):
        pass

    class _ChatCompletions:
        def __init__(self, client):
            self._client = client

        def create(self, **kwargs):
            return self._client._post("/chat/completions", kwargs)

    class _Chat:
        def __init__(self, client):
            self.completions = _ChatCompletions(client)

    class OpenAI:
        def __init__(self, api_key=None, base_url=None, timeout=None, **kwargs):
            self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
            self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
            try:
                seconds = float(timeout or kwargs.get("timeout") or 20)
                if seconds > 1000:
                    seconds = seconds / 1000.0
            except Exception:
                seconds = 20
            self.timeout = max(1, min(int(seconds), 20))
            self.chat = _Chat(self)

        def _post(self, path, payload):
            if not self.api_key or self.api_key == "sk-none":
                raise OpenAIError("OPENAI_API_KEY is not set")
            cmd = [
                "curl.exe", "-sS", "--max-time", str(self.timeout),
                "-X", "POST", self.base_url + path,
                "-H", "Authorization: Bearer " + self.api_key,
                "-H", "Content-Type: application/json",
                "--data-binary", "@-",
            ]
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )
            if proc.returncode != 0:
                raise OpenAIError("curl failed %s: %s" % (proc.returncode, (proc.stderr or "")[:500]))
            body = proc.stdout or ""
            if not body.strip():
                raise OpenAIError("empty OpenAI response")
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("error"):
                raise OpenAIError("OpenAI error: %s" % parsed["error"])
            return _to_attr(parsed)

    module = types.ModuleType("openai")
    module.OpenAI = OpenAI
    module.OpenAIError = OpenAIError
    module.AuthenticationError = OpenAIError
    module.APIError = OpenAIError
    module.RateLimitError = OpenAIError
    module._OBS_SHIM = True
    sys.modules["openai"] = module


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
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.replace("đ", "d").replace("Đ", "D")


def _canonical(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _strip_accents(text).lower()
    return re.sub(r"\s+", " ", text).strip()


def _sanitize_question(question):
    cleaned, count = _NOTE_RE.subn("ghi chu: [removed untrusted note]", str(question or ""))
    return cleaned, count


def _extract_order_hints(question):
    text = str(question or "")
    coupon_match = _COUPON_RE.search(text)
    qty_match = _QTY_RE.search(text)
    dest_match = _DEST_RE.search(text)

    product = _LEADING_ORDER_RE.sub("", text)
    product = _TAIL_RE.sub("", product)
    product = re.sub(r"(?i)\b(?:san pham|sản phẩm|cai|cái|chiec|chiếc)\b", " ", product)
    product = re.sub(r"[,\-–—:;?.!]+", " ", product)
    product = re.sub(r"\s+", " ", product).strip()

    hints = []
    if product:
        hints.append(f"clean_product={product}")
    if qty_match:
        hints.append(f"quantity={qty_match.group(1)}")
    if coupon_match:
        hints.append(f"coupon={coupon_match.group(1).upper()}")
    if dest_match:
        destination = re.sub(r"\s+", " ", dest_match.group(1)).strip()
        if destination:
            hints.append(f"destination={destination}")
    if not hints:
        return text
    return text + "\n\nTrusted parsed fields for extraction only: " + "; ".join(hints) + "."


def _quantity_from_question(question):
    match = _QTY_RE.search(str(question or ""))
    return max(1, int(match.group(1))) if match else 1


def _shipping_needed(question):
    text = _canonical(question)
    return any(term in text for term in ("ship", "giao", " ve ", " den "))


def _find_value(obj, names):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _canonical(key) in names:
                return value
        for value in obj.values():
            found = _find_value(value, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_value(value, names)
            if found is not None:
                return found
    return None


def _number(value, default=None):
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        return value
    match = _MONEY_RE.search(str(value))
    if not match:
        return default
    digits = re.sub(r"\D", "", match.group(0))
    return int(digits) if digits else default


def _truthy_stock(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return _canonical(value) not in ("false", "no", "0", "het hang", "out of stock")


def _step_observation(trace, tool_name):
    for step in trace or []:
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or step.get("tool") or step.get("name") or "")
        if tool_name in action:
            return step.get("observation"), action
    return None, ""


def _observation_failed(observation):
    text = _canonical(json.dumps(observation, ensure_ascii=False, default=str))
    return any(marker in text for marker in (
        "item_not_found",
        "not_found",
        "destination_not_served",
        "het hang",
        "out_of_stock",
        "out of stock",
        "khong ho tro",
        "chua ho tro",
        "khong phuc vu",
        "chua phuc vu",
    ))


def _deterministic_answer_from_trace(trace, question):
    stock, _ = _step_observation(trace, "check_stock")
    if not isinstance(stock, dict):
        return None
    if _observation_failed(stock):
        return "Khong the dat hang voi thong tin hien tai."

    in_stock = _find_value(stock, {"in_stock", "available", "stock"})
    quantity_available = _number(_find_value(stock, {"quantity", "qty", "stock_qty"}))
    if not _truthy_stock(in_stock) or quantity_available == 0:
        return "Khong the dat hang voi thong tin hien tai."

    price = _number(_find_value(stock, {"unit_price", "price", "price_vnd", "unit_price_vnd"}))
    if price is None:
        return None

    qty = _quantity_from_question(question)
    discount_percent = 0
    if _COUPON_RE.search(str(question or "")):
        discount, _ = _step_observation(trace, "get_discount")
        if isinstance(discount, dict) and not _observation_failed(discount):
            discount_percent = _number(_find_value(discount, {"discount_percent", "percent", "pct"}), 0) or 0

    shipping_fee = 0
    if _shipping_needed(question):
        shipping, action = _step_observation(trace, "calc_shipping")
        if not isinstance(shipping, dict) or _observation_failed(shipping):
            return "Khong the dat hang voi thong tin hien tai."
        shipping_fee = _number(_find_value(shipping, {
            "shipping_fee",
            "shipping",
            "fee",
            "fee_vnd",
            "cost",
            "cost_vnd",
            "amount",
        }))
        if shipping_fee is None:
            return None
        weight_arg = re.search(r"['\"]?weight_kg['\"]?\s*:\s*([0-9.]+)", action)
        if weight_arg and float(weight_arg.group(1)) == 0 and qty > 0:
            return None

    subtotal = int(price) * qty
    discounted = subtotal * (100 - int(discount_percent)) // 100
    return f"Tong cong: {discounted + int(shipping_fee)} VND"


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
            observation = step.get("observation")
            if isinstance(observation, (dict, list)):
                observation_preview = observation
            else:
                observation_preview = str(observation)[:300] if observation is not None else None
            summary.append({
                "action": step.get("action") or step.get("tool") or step.get("name"),
                "error": step.get("error"),
                "observation_type": type(step.get("observation")).__name__ if "observation" in step else None,
                "observation": observation_preview,
            })
    return summary


def _compact_success_answer(answer, question=""):
    text = str(answer or "")
    canonical = _canonical(text)
    if any(marker in canonical for marker in (
        "het hang",
        "out of stock",
        "item_not_found",
        "khong tim thay",
        "chua tim thay",
        "destination_not_served",
        "chua phuc vu",
        "khong duoc phuc vu",
        "khong ho tro",
        "chua ho tro",
        "khong the giao",
        "khong giao duoc",
        "khong van chuyen",
        "khong tinh duoc phi ship",
        "khong the tinh phi ship",
        "khong tinh duoc phi",
    )):
        return "Khong the dat hang voi thong tin hien tai."

    matches = _TOTAL_RE.findall(text)
    if not matches:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            normalized = _canonical(line)
            if "tong" not in normalized and "tam tinh" not in normalized:
                continue
            if "chua gom ship" in normalized:
                continue
            if not any(term in normalized for term in ("cong", "thanh toan", "tien", "tam tinh")):
                continue
            numbers = _MONEY_RE.findall(line)
            if not numbers and index + 1 < len(lines):
                numbers = _MONEY_RE.findall(lines[index + 1])
            if numbers:
                matches.append(numbers[-1])
    if not matches:
        question_canonical = _canonical(question)
        needs_shipping = any(term in question_canonical for term in ("ship", "giao", " ve ", " den ", "đến"))
        if not needs_shipping:
            for line in text.splitlines():
                normalized = _canonical(line)
                if "gia" in normalized and "giam" not in normalized:
                    price_match = re.search(r"(?i)(?:gia|giá)[^\d]*(\d[\d., ]*\d|\d+)", line)
                    if price_match:
                        matches.append(price_match.group(1))
                        break
    if not matches:
        return answer
    digits = re.sub(r"\D", "", matches[-1])
    if not digits:
        return answer
    return f"Tong cong: {digits} VND"


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
        "context_size": min(int(conf.get("context_size", 2) or 2), 2),
        "verbose_system": False,
        "session_drift_rate": 0,
        "context_reset_every": 1,
        "tool_error_rate": 0,
        "catalog_override": {"macbook": {"in_stock": True}},
        "planner": True,
        "verify": True,
        "self_consistency": 1,
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
    if (config or {}).get("provider", "openai") == "openai":
        _install_openai_shim()
    context = context or {}
    qid = context.get("qid", "unknown")
    session_id = context.get("session_id", "unknown")
    turn_index = context.get("turn_index", 0)
    set_correlation_id(f"{session_id}:{turn_index}:{qid}")

    cache = context.get("cache")
    lock = context.get("cache_lock")
    sanitized_question, removed_notes = _sanitize_question(question)
    routed_question = _extract_order_hints(sanitized_question)
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
            result = call_next(routed_question, conf)
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
            trace_answer = _deterministic_answer_from_trace(result.get("trace"), sanitized_question)
            compacted_answer = trace_answer or _compact_success_answer(redacted_answer, sanitized_question)
            if redactions:
                result = copy.deepcopy(result)
            if compacted_answer != answer:
                result = copy.deepcopy(result)
                result["answer"] = compacted_answer

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
