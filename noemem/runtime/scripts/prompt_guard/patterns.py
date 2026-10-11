"""Fallback pattern-based prompt injection detector.

Used when the Prompt-Guard model is not yet available (gated repo pending
approval) or when the model fails to load.

Uses regex + substring patterns known to indicate prompt injection attempts.
Deliberately conservative — false positive on benign content is better than
false negative on an injection.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Each pattern: (name, compiled_regex, description, base_severity)
_PATTERNS: List[Tuple[str, re.Pattern, str, float]] = [
    # --- Classic injection patterns ---
    ("ignore_previous",
     re.compile(
         r"\b(?:ignore|disregard|forget|drop|override|bypass|overwrite|discard)"
         r"(?:\s+(?:all|any|the|your|previous|above|prior|earlier|given))?"
         r"(?:\s+(?:instructions|directives|commands|prompts|rules|guidelines|"
         r"context|system\s*prompt|constraints|orders))?\b",
         re.IGNORECASE,
     ),
     "Attempt to ignore/overwrite previous instructions", 0.8),

    ("new_role",
     re.compile(
         r"\bfrom\s+now\s+on\s*(?:,)?\s*(?:you\s+(?:are|will|must)\s+(?:a|an|the)"
         r"|act\s+(?:as|like)\s+(?:a|an|the)"
         r"|pretend\s+(?:to\s+be|that))\b",
         re.IGNORECASE,
     ),
     "Role-play hijacking attempt (from now on)", 0.8),

    ("reset_role",
     re.compile(
         r"\b(?:"
         r"(?:now|from\s+now\s+on)\s*(?:,)?\s*"
         r"(?:you\s+(?:are|will|must)"
         r"(?:\s+(?:no\s+longer|not|free\s+from|released\s+from|unleashed))?"
         r"(?:\s+(?:a|an|the)\s+)?(?:unrestricted|malicious|evil|free|DAN|jail)"
         r"|act\s+(?:as|like)\s+(?:a|an|the)?\s*(?:DAN|evil|malicious|unrestricted"
         r"|free|rebel)"
         r")\b"
         r"|"
         r"\bact\s+(?:as|like)\s+(?:a|an|the)?\s*(?:DAN|evil|malicious|unrestricted"
         r"|free|rebel)\b"
         r")",
         re.IGNORECASE,
     ),
     "Role reset / jailbreak attempt", 0.9),

    ("new_identity",
     re.compile(
         r"\byou\s+(?:are|will\s+be)\s+(?:now\s+)?(?:a|an|the)\s+"
         r"(?:new\s+|"
         r"unrestricted\s+|malicious\s+|evil\s+|free\s+|"
         r"neomezený\s+|zlý\s+|volný\s+|svobodný\s+)?"
         r"(?:AI|assistant|bot|entity|character|persona|version)"
         r"(?:\s+(?:with|without|that|who|which))?",
         re.IGNORECASE,
     ),
     "New identity / role assignment", 0.7),

    ("system_prompt_extract",
     re.compile(
         r"\b(?:show|reveal|display|print|output|leak|expose|give|tell)\s+"
         r"(?:me\s+)?(?:your|the|my|this)?\s*(?:system\s+)?(?:prompt|"
         r"instructions|directives|system\s+message|initial\s+prompt|"
         r"base\s+prompt|constitution)\b",
         re.IGNORECASE,
     ),
     "Attempt to extract system prompt", 0.8),

    # --- DAN / jailbreak patterns ---
    ("dan_mode",
     re.compile(
         r"\bDAN\b|do\s+anything\s+now|jail\s*(?:break|broke)"
         r"|fuck\s+(?:the\s+)?(?:rules|guidelines|restrictions)"
         r"|ignore\s+(?:all\s+)?(?:ethical|moral|legal|safety)\s+(?:"
         r"constraints|guidelines|boundaries|restrictions)",
         re.IGNORECASE,
     ),
     "DAN / jailbreak mode attempt", 0.9),

    # --- Hypnotic / prompt-leaking patterns ---
    ("hypnotic",
     re.compile(
         r"\byou\s+have\s+been\s+(?:programmed|trained|configured|set\s+up)\s+to"
         r"|your\s+(?:core\s+)?(?:purpose|function|directive|programming)\s+is"
         r"|you\s+must\s+(?:always|never|obey|follow)\s+(?:the\s+)?(?:user'?s|"
         r"my|these)\s+(?:instructions|commands|orders)",
         re.IGNORECASE,
     ),
     "Hypnotic / prompt-leaking pattern", 0.7),

    # --- Output-chaining / injection via embedded instructions ---
    ("embedded_instructions",
     re.compile(
         r"\b(?:first|now|next|then|finally|after\s+that|before\s+continuing)"
         r"(?:\s*[,:])?\s*(?:ignore|do\s+not|never|always|make\s+sure|"
         r"remember\s+to|ensure\s+you|be\s+sure\s+to)\b",
         re.IGNORECASE,
     ),
     "Embedded instructions in third-party content", 0.6),

    # --- Data URI / base64 payloads (common in injection attacks) ---
    ("data_uri_payload",
     re.compile(
         r"data\s*:\s*(?:image|text|application)\s*/[a-z+.-]+;\s*base64\s*[,;]"
         r"|[A-Za-z0-9+/]{100,}={0,2}\s*(?:ignore|forget|do\s+not)",
         re.IGNORECASE,
     ),
     "Base64 / data URI payload with instructions", 0.8),

    # --- Meta-instructions in fetched content ---
    ("meta_instruction",
     re.compile(
         r"\b(?:as\s+(?:a\s+)?(?:language\s+model|AI|assistant|bot),?\s+)"
         r"(?:you|your)\s+(?:must|should|will|are\s+(?:to|now))\b",
         re.IGNORECASE,
     ),
     "Meta-instructions addressed to the AI model", 0.7),

    # --- Czech language patterns ---
    ("czech_ignore",
     re.compile(
         r"\b(?:ignoruj|zapomeň|přeskoč|zruš|přepiš|odstraň|nevšímej)\s*"
         r"(?:si\s+)?(?:předchozí|všechny|tyto|veškeré|dané|své)?"
         r"(?:\s+(?:instrukce|příkazy|pokyny|direktivy|pravidla|omezení"
         r"|nastavení|příkazů|instrukcí|pokynů))?\b",
         re.IGNORECASE,
     ),
     "Czech: attempt to ignore/overwrite instructions", 0.8),

    ("czech_system_prompt",
     re.compile(
         r"\b(?:ukaž|zobraz|vypiš|řekni|prozraď|dej)\s*(?:mi\s+)?"
         r"(?:svůj|své|tvůj|ten|můj)?\s*(?:system\s+)?(?:prompt|instrukce"
         r"|nastavení|pokyny|příkazy|systemovej\s+prompt)\b",
         re.IGNORECASE,
     ),
     "Czech: attempt to extract system prompt", 0.8),

    ("czech_jailbreak",
     re.compile(
         r"\b(?:jsi\s+teď|od\s+ted'\s+(?:jsi|budeš)|od\s+této\s+chvíle\s+(?:"
         r"jsi|budeš)|teď\s+(?:jsi|budeš))\s*(?:volný|svobodný|bez\s+omezení"
         r"|nový|zlý|maligní|neomezený)\s*(?:AI|asistent|bot|entita)?\b"
         r"|chovej\s+se\s+jako\s+DAN\b",
         re.IGNORECASE,
     ),
     "Czech: jailbreak / role reset", 0.9),
]


def scan(text: str) -> List[Tuple[str, str, float]]:
    """Scan text for prompt injection patterns.

    Returns:
        List of (pattern_name, description, severity) tuples for matches.
        Severity is based on the pattern's base severity, boosted by match
        count.
    """
    if not text:
        return []

    hits: List[Tuple[str, str, float]] = []
    for name, regex, desc, base_sev in _PATTERNS:
        matches = regex.findall(text)
        if matches:
            boost = min(0.3, len(matches) * 0.1)
            severity = min(1.0, base_sev + boost)
            hits.append((name, desc, severity))

    return hits


def get_indirect_injection_score(text: str) -> float:
    """Return combined score (0-1) for indirect injection risk."""
    hits = scan(text)
    if not hits:
        return 0.0
    return max(s for _, _, s in hits)


def get_jailbreak_score(text: str) -> float:
    """Return score (0-1) for jailbreak risk with weighted patterns."""
    hits = scan(text)
    if not hits:
        return 0.0

    jailbreak_weights = {
        "dan_mode": 2.0,
        "ignore_previous": 1.5,
        "system_prompt_extract": 1.5,
        "czech_jailbreak": 2.0,
        "czech_ignore": 1.5,
        "czech_system_prompt": 1.5,
    }

    max_score = 0.0
    for name, _, severity in hits:
        weight = jailbreak_weights.get(name, 1.0)
        max_score = max(max_score, min(1.0, severity * weight))

    return max_score
