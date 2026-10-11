#!/usr/bin/env python3
"""fcntl-based FIFO zámek pro lokální LLM API (:8085).

Použití:
    from api_lock import with_api_lock

    @with_api_lock
    def call_llm(prompt):
        resp = requests.post("http://localhost:8085/v1/chat/completions", ...)
        return resp.json()

FIFO fronta — kernel sám serializuje přístup v pořadí, v jakém procesy
o zámek požádaly. Žádný timeout, žádný fail. Lock se uvolní automaticky
při close (i při crash procesu).

Žádná závislost — jen Python stdlib (fcntl, logging, functools).
"""
import fcntl
import logging
from functools import wraps

LOCK_FILE = "/tmp/hermes-api-8085.lock"
logger = logging.getLogger(__name__)


def with_api_lock(func):
    """Dekorátor: funkci obalí fcntl flock — volání se zablokuje,
    dokud není API volné. FIFO fronta v kernelu."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        with open(LOCK_FILE, "a") as f:
            logger.info("⏳ API zámek: čekám (%s)", LOCK_FILE)
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            logger.info("✅ API zámek získán (%s)", LOCK_FILE)
            try:
                return func(*args, **kwargs)
            finally:
                # lock se uvolní při close — probudí dalšího ve frontě
                pass
    return wrapper
