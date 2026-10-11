"""Core Prompt-Guard classifier — model backend + fallback.

Two backends:
1. ModelBackend: loads meta-llama/Prompt-Guard-86M via transformers (gated)
2. PatternBackend: regex-based fallback used when model is unavailable

Auto-selects: tries model first, falls back to patterns on failure.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

from . import config as C
from .patterns import get_indirect_injection_score as _pattern_indirect_score
from .patterns import get_jailbreak_score as _pattern_jailbreak_score
from .patterns import scan as _pattern_scan

logger = logging.getLogger("prompt-guard")

# Whether to use the Prompt-Guard-86M model backend (opt-in).
# The model has high false-positive rate on English text without fine-tuning.
# By default only the pattern-based fallback is used (zero FP).
# Set PROMPT_GUARD_MODEL_BACKEND=1 to enable the model as secondary check.
_USE_MODEL_BACKEND = os.environ.get("PROMPT_GUARD_MODEL_BACKEND", "").lower() in {
    "1", "true", "yes",
}


# ---------------------------------------------------------------------------
# Meta's preprocessing — removes spaces to prevent token-splitting bypass
# ---------------------------------------------------------------------------


def _preprocess_text(text: str, tokenizer) -> str:
    """Remove spaces, tokenize, reconstruct with original spacing.

    This prevents attackers from inserting spaces to break token-based
    detection. Used by Prompt-Guard-86M inference code.
    """
    try:
        cleaned_text = ""
        index_map = []
        for i, char in enumerate(text):
            if not char.isspace():
                cleaned_text += char
                index_map.append(i)

        tokens = tokenizer.tokenize(cleaned_text)
        result = []
        last_end = 0
        for token in tokens:
            token_str = tokenizer.convert_tokens_to_string([token])
            start = cleaned_text.index(token_str, last_end)
            end = start + len(token_str)
            original_start = index_map[start]
            if original_start > 0 and text[original_start - 1].isspace():
                result.append(" ")
            result.append(token_str)
            last_end = end
        return "".join(result)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Model backend (transformers)
# ---------------------------------------------------------------------------

class ModelBackend:
    """Loads Prompt-Guard-86M via transformers and provides classification.

    Lazy-loaded on first call. Returns None for all methods if model fails
    to load.
    """

    def __init__(self):
        self._model = None
        self._tokenizer = None
        self._loaded = False
        self._load_error: Optional[str] = None

    def _load(self) -> bool:
        if self._loaded:
            return True
        if self._load_error:
            return False

        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            import torch

            t0 = time.time()
            logger.info("Loading Prompt-Guard-86M model...")

            token = C.resolve_hf_token()

            self._tokenizer = AutoTokenizer.from_pretrained(
                C.MODEL_ID,
                token=token,
                cache_dir=str(C.HF_HOME),
            )
            self._model = AutoModelForSequenceClassification.from_pretrained(
                C.MODEL_ID,
                token=token,
                cache_dir=str(C.HF_HOME),
            )
            self._model.eval()

            dt = time.time() - t0
            logger.info(f"Prompt-Guard loaded in {dt:.2f}s")
            self._loaded = True
            return True

        except Exception as e:
            msg = str(e)
            # Check for gated-repo error specifically
            if "gated" in msg.lower() or "403" in msg or "401" in msg:
                self._load_error = "gated"
                logger.warning(
                    "Prompt-Guard-86M is gated and not yet approved. "
                    "Falling back to pattern detector."
                )
            else:
                self._load_error = msg
                logger.warning(f"Failed to load Prompt-Guard model: {msg}")
            return False

    def classify(self, text: str) -> Dict[str, float]:
        """Return {label: score} dict for the three classes.

        Labels: BENIGN (0), INJECTION (1), JAILBREAK (2)
        """
        if not self._load():
            return {}

        from torch.nn.functional import softmax
        import torch

        # Chunk long text
        chunks = [
            text[i : i + C.CHUNK_SIZE]
            for i in range(0, min(len(text), C.MAX_SCAN_CHARS), C.CHUNK_SIZE)
        ]

        max_probs = [0.0, 0.0, 0.0]

        for chunk in chunks:
            # Apply Meta's preprocessing to prevent token-splitting bypass
            processed = _preprocess_text(chunk, self._tokenizer)
            inputs = self._tokenizer(
                processed,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=512,
            )
            with torch.no_grad():
                logits = self._model(**inputs).logits
            scaled = logits / C.SOFTMAX_TEMPERATURE
            probs = softmax(scaled, dim=-1)[0].tolist()

            for i in range(3):
                max_probs[i] = max(max_probs[i], probs[i])

        return {
            "BENIGN": round(max_probs[0], 4),
            "INJECTION": round(max_probs[1], 4),
            "JAILBREAK": round(max_probs[2], 4),
        }

    def get_indirect_injection_score(self, text: str) -> float:
        """Combined probability of label 1 (INJECTION) + label 2 (JAILBREAK)."""
        probs = self.classify(text)
        if not probs:
            return -1.0  # signal fallback
        return round(probs["INJECTION"] + probs["JAILBREAK"], 4)

    def get_jailbreak_score(self, text: str) -> float:
        """Probability of label 2 (JAILBREAK) only."""
        probs = self.classify(text)
        if not probs:
            return -1.0
        return round(probs["JAILBREAK"], 4)

    @property
    def available(self) -> bool:
        return self._load()

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error


# ---------------------------------------------------------------------------
# Unified guard interface
# ---------------------------------------------------------------------------

_model_backend = ModelBackend()


def _use_model() -> bool:
    """Check if model backend is available AND enabled via PROMPT_GUARD_MODEL_BACKEND."""
    return _USE_MODEL_BACKEND and _model_backend.available


def classify(text: str) -> Dict[str, float]:
    """Full classification: {BENIGN: 0.99, INJECTION: 0.01, JAILBREAK: 0.00}."""
    if _use_model():
        return _model_backend.classify(text)
    # Fallback: heuristic severity → pseudo-probabilities
    prob_benign = 1.0
    prob_injection = 0.0
    prob_jailbreak = 0.0
    hits = scan(text)
    for _, _, severity in hits:
        if severity > 0.8:
            prob_jailbreak = max(prob_jailbreak, severity)
        else:
            prob_injection = max(prob_injection, severity)
    prob_benign = max(0.0, 1.0 - prob_injection - prob_jailbreak)
    total = prob_benign + prob_injection + prob_jailbreak
    if total > 0:
        prob_benign /= total
        prob_injection /= total
        prob_jailbreak /= total
    return {
        "BENIGN": round(prob_benign, 4),
        "INJECTION": round(prob_injection, 4),
        "JAILBREAK": round(prob_jailbreak, 4),
    }


def scan(text: str) -> List[Tuple[str, str, float]]:
    """Pattern scan (always available, model-independent)."""
    return _pattern_scan(text)


def get_indirect_injection_score(text: str) -> float:
    """Combined injection + jailbreak probability (0–1)."""
    if _use_model():
        s = _model_backend.get_indirect_injection_score(text)
        if s >= 0:
            return s
    return _pattern_indirect_score(text)


def get_jailbreak_score(text: str) -> float:
    """Jailbreak-only probability (0–1)."""
    if _use_model():
        s = _model_backend.get_jailbreak_score(text)
        if s >= 0:
            return s
    return _pattern_jailbreak_score(text)


def check(
    text: str,
    mode: str = "indirect",
    threshold: float = None,
    source: str = "cli",
) -> Dict:
    """High-level check: returns verdict dict.

    Two-stage hybrid detection:
      1. Pattern scan (fast, deterministic, zero false positives)
      2. Model scan (only if pattern missed and model is available — catches
         subtle injections patterns can't detect)

    Args:
        text: Content to classify.
        mode: "indirect" for third-party content, "jailbreak" for user prompts.
        threshold: Score threshold (default from config).

    Returns:
        {
            "verdict": "BENIGN" | "FLAGGED",
            "label": "BENIGN" | "INJECTION" | "JAILBREAK",
            "score": float,
            "indirect_score": float,
            "jailbreak_score": float,
            "patterns": [{"name": str, "desc": str, "severity": float}],
            "model_used": "pattern-fallback" | "hybrid",
            "is_safe": bool,
            "chunks": int,
        }
    """
    if threshold is None:
        threshold = (
            C.DEFAULT_INDIRECT_THRESHOLD if mode == "indirect"
            else C.DEFAULT_JAILBREAK_THRESHOLD
        )

    chunks = max(1, (len(text) + C.CHUNK_SIZE - 1) // C.CHUNK_SIZE)

    # --- Stage 1: Pattern scan (fast, zero FP) ---
    pattern_hits = scan(text)
    pattern_score = (
        _pattern_indirect_score(text) if mode == "indirect"
        else _pattern_jailbreak_score(text)
    )

    # --- Stage 2: Model scan (only if pattern missed it) ---
    model_used = "pattern-fallback"
    model_score = 0.0
    model_indirect = 0.0
    model_jailbreak = 0.0
    model_probs = {"BENIGN": 1.0, "INJECTION": 0.0, "JAILBREAK": 0.0}

    if pattern_score < threshold and _use_model():
        model_indirect = get_indirect_injection_score(text)
        model_jailbreak = get_jailbreak_score(text)
        model_probs = classify(text)

        if mode == "indirect":
            model_score = model_indirect
        else:
            model_score = model_jailbreak

        if model_score >= threshold:
            model_used = "hybrid"

    # --- Combine scores ---
    score = max(pattern_score, model_score)
    indirect_score = max(
        pattern_score if mode == "indirect" else 0.0,
        model_indirect,
    )
    jailbreak_score = max(
        pattern_score if mode == "jailbreak" else 0.0,
        model_jailbreak,
    )

    # --- Determine label ---
    label = "BENIGN"
    if score >= threshold:
        # Prioritize jailbreak if score is high enough from either source
        if jailbreak_score >= threshold:
            label = "JAILBREAK"
        elif indirect_score >= threshold:
            label = "INJECTION"

    is_safe = label == "BENIGN"

    # --- Watch stage: Laya-MLX first-stage classifier (observability only) ---
    laya_result = None
    try:
        from . import laya_backend
        laya_result = laya_backend.classify(text[:4000], source=source)
    except Exception:
        laya_result = None

    return {
        "verdict": "BENIGN" if is_safe else "FLAGGED",
        "label": label,
        "score": round(score, 4),
        "indirect_score": round(indirect_score, 4),
        "jailbreak_score": round(jailbreak_score, 4),
        "probabilities": model_probs,
        "patterns": [
            {"name": n, "description": d, "severity": round(s, 4)}
            for n, d, s in pattern_hits
        ],
        "model_used": model_used,
        "is_safe": is_safe,
        "chars": len(text),
        "chunks": chunks,
        "laya": laya_result,
    }
