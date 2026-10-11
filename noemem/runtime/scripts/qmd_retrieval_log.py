#!/usr/bin/env python3
"""Append-only QMD retrieval telemetry + light parsers.

Log path: ~/hermes-noemem/logs/qmd-retrieval-YYYY-MM.jsonl
Fail-open: never raise into the agent hot path.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LOG_DIR = Path.home() / "hermes-noemem" / "logs"
_LOCK = threading.Lock()

_LAYER_RULES = (
    ("memory", ("hermes-noemem-memory", "/memory/", "memory/")),
    ("current", ("hermes-noemem-projekty", "/projekty/", "-current.md")),
    ("archive", ("hermes-noemem-archive", "/archive/", "archive/")),
    ("agent", ("hermes-noemem-agent", "/agent/")),
    ("personal", ("hermes-noemem-personal", "/personal/")),
)


def log_path_for(ts: Optional[datetime] = None) -> Path:
    dt = ts or datetime.now(timezone.utc)
    return LOG_DIR / f"qmd-retrieval-{dt.strftime('%Y-%m')}.jsonl"


def classify_layer(path: str) -> str:
    p = (path or "").lower().replace("\\", "/")
    for name, needles in _LAYER_RULES:
        if any(n.lower() in p for n in needles):
            return name
    if not p:
        return "none"
    return "other"


def layer_top1(hits: Iterable[Dict[str, Any]]) -> str:
    for h in hits:
        path = str(h.get("path") or h.get("file") or "")
        if path:
            return classify_layer(path)
    return "none"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_event(event: Dict[str, Any]) -> None:
    """Write one JSONL event. Never raises."""
    try:
        payload = dict(event)
        payload.setdefault("ts", _iso_now())
        hits = payload.get("hits") or []
        if not isinstance(hits, list):
            hits = []
        # Cap stored hits
        slim_hits: List[Dict[str, Any]] = []
        for h in hits[:8]:
            if not isinstance(h, dict):
                continue
            slim_hits.append(
                {
                    "path": str(h.get("path") or h.get("file") or "")[:400],
                    "score": h.get("score"),
                    "title": str(h.get("title") or "")[:200] or None,
                    "docid": h.get("docid"),
                }
            )
        payload["hits"] = slim_hits
        payload["n_hits"] = len(slim_hits)
        # Truth: empty when zero hits. Callers must not force empty=false with no hits.
        payload["empty"] = payload["n_hits"] == 0
        payload["layer_top1"] = payload.get("layer_top1") or layer_top1(slim_hits)

        q = payload.get("query")
        if isinstance(q, str) and len(q) > 800:
            payload["query"] = q[:800]

        path = log_path_for()
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return


def parse_hits_from_qmd_text(text: str) -> List[Dict[str, Any]]:
    """Best-effort extract hits from QMD MCP text/JSON payloads."""
    if not text or not str(text).strip():
        return []
    raw = str(text).strip()

    # structuredContent / JSON array
    try:
        data = json.loads(raw)
        hits = _hits_from_json(data)
        if hits:
            return hits
    except Exception:
        pass

    # Often wrapped: {"result": "..."} or MCP content envelope
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in ("result", "content", "text", "structuredContent"):
                if key in data:
                    nested = data[key]
                    if isinstance(nested, str):
                        try:
                            nested_j = json.loads(nested)
                            hits = _hits_from_json(nested_j)
                            if hits:
                                return hits
                        except Exception:
                            pass
                        hits = _hits_from_plain(nested)
                        if hits:
                            return hits
                    else:
                        hits = _hits_from_json(nested)
                        if hits:
                            return hits
    except Exception:
        pass

    return _hits_from_plain(raw)


def _hits_from_json(data: Any) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            data = data["results"]
        elif "structuredContent" in data and isinstance(data["structuredContent"], dict):
            sc = data["structuredContent"]
            if isinstance(sc.get("results"), list):
                data = sc["results"]
    if not isinstance(data, list):
        return hits
    for item in data:
        if not isinstance(item, dict):
            continue
        path = (
            item.get("file")
            or item.get("path")
            or item.get("source")
            or item.get("uri")
            or ""
        )
        score = item.get("score")
        if score is None:
            score = item.get("relevance")
        hits.append(
            {
                "path": str(path),
                "score": score,
                "title": item.get("title"),
                "docid": item.get("docid") or item.get("id"),
            }
        )
    return hits


_HIT_LINE_RE = re.compile(
    r"(?:^|\n)#(?P<docid>[0-9a-fA-F]+)\s+"
    r"(?P<score>\d{1,3})%\s+"
    r"(?P<path>\S+)"
    r"(?:\s+-\s+(?P<title>.+))?",
    re.MULTILINE,
)
_PATHISH_RE = re.compile(
    r"(hermes-noemem[\w-]*/[^\s]+?\.md|[^\s]+-current\.md|memory/[^\s]+?\.md)",
    re.IGNORECASE,
)


def _hits_from_plain(text: str) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for m in _HIT_LINE_RE.finditer(text):
        score_pct = m.group("score")
        score = float(score_pct) / 100.0 if score_pct else None
        hits.append(
            {
                "path": m.group("path"),
                "score": score,
                "title": (m.group("title") or "").strip() or None,
                "docid": f"#{m.group('docid')}",
            }
        )
    if hits:
        return hits
    # Fallback: unique path-like tokens
    seen = set()
    for m in _PATHISH_RE.finditer(text):
        p = m.group(1)
        if p in seen:
            continue
        seen.add(p)
        hits.append({"path": p, "score": None, "title": None, "docid": None})
        if len(hits) >= 8:
            break
    return hits


def summarize_query_from_mcp_args(args: Optional[dict]) -> Dict[str, Any]:
    """Normalize mcp_qmd_* args into query/searches/collections."""
    args = args if isinstance(args, dict) else {}
    searches = args.get("searches")
    query_parts: List[str] = []
    norm_searches: List[Dict[str, str]] = []
    if isinstance(searches, list):
        for s in searches:
            if not isinstance(s, dict):
                continue
            q = str(s.get("query") or "")[:500]
            t = str(s.get("type") or "")
            if q:
                query_parts.append(q)
                norm_searches.append({"type": t, "query": q})
    file_arg = args.get("file") or args.get("pattern") or args.get("uri")
    if file_arg and not query_parts:
        query_parts.append(str(file_arg)[:500])
    collections = args.get("collections")
    if collections is not None and not isinstance(collections, list):
        collections = [collections]
    return {
        "query": " | ".join(query_parts)[:800],
        "searches": norm_searches,
        "collections": collections,
        "intent": str(args.get("intent") or "")[:400] or None,
    }
