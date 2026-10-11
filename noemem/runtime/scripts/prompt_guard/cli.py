#!/usr/bin/env python3
"""CLI wrapper for Prompt-Guard classification.

Usage:
    prompt-guard check [--mode indirect|jailbreak] [--threshold 0.7] -- "text to check"
    echo "some text" | prompt-guard check
    prompt-guard check --file /path/to/content.txt

Exit codes:
    0 — content is safe (BENIGN)
    1 — content is FLAGGED
    2 — error
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config as C
from .guard import check


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Prompt-Guard classifier — detect prompt injections and jailbreaks",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ── check ──────────────────────────────────────────────────────────
    check_p = sub.add_parser("check", help="Classify text for prompt injection risk")
    check_p.add_argument(
        "--mode",
        choices=["indirect", "jailbreak"],
        default="indirect",
        help="'indirect' (default) for third-party content (web, search). "
             "'jailbreak' for user prompts.",
    )
    check_p.add_argument(
        "--threshold",
        type=float,
        default=None,
        help=f"Score threshold (default: indirect={C.DEFAULT_INDIRECT_THRESHOLD}, "
             f"jailbreak={C.DEFAULT_JAILBREAK_THRESHOLD})",
    )
    check_p.add_argument("--file", help="Read text from file instead of stdin/arg")
    check_p.add_argument(
        "--text", nargs="*",
        help="Text to classify (pass as positional or via --text)",
    )
    check_p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show detailed output including patterns and probabilities",
    )

    # ── status ─────────────────────────────────────────────────────────
    status_p = sub.add_parser("status", help="Show backend status")
    status_p.add_argument("-v", "--verbose", action="store_true")

    return p


def cmd_check(args: argparse.Namespace) -> int:
    if args.file:
        with open(args.file) as f:
            text = f.read()
    elif args.text:
        text = " ".join(args.text)
    else:
        text = sys.stdin.read()

    if not text or not text.strip():
        print(json.dumps({"error": "No input text provided"}), file=sys.stderr)
        return 2

    result = check(text, mode=args.mode, threshold=args.threshold,
                   source=f"cli:{args.file}" if args.file else "cli")

    if args.verbose:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        # Compact output
        print(json.dumps({
            "verdict": result["verdict"],
            "score": result["score"],
            "label": result["label"],
            "is_safe": result["is_safe"],
            "model": result["model_used"],
        }, ensure_ascii=False))

    return 0 if result["is_safe"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    from .guard import _model_backend, _use_model

    info = {
        "model": {
            "id": C.MODEL_ID,
            "available": _model_backend.available,
        },
        "active_backend": "prompt-guard-86m" if _use_model() else "pattern-fallback",
        "config": {
            "indirect_threshold": C.DEFAULT_INDIRECT_THRESHOLD,
            "jailbreak_threshold": C.DEFAULT_JAILBREAK_THRESHOLD,
            "hf_home": str(C.HF_HOME),
            "max_scan_chars": C.MAX_SCAN_CHARS,
        },
    }

    if args.verbose:
        info["model"]["load_error"] = _model_backend.load_error

    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "status":
        return cmd_status(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
