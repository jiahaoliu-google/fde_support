#!/usr/bin/env python3
"""Gate 4: Post-deploy smoke test for Vertex AI Agent Runtime engines.

Sends live authenticated `:streamQuery` probes to the deployed Sentiment Router
and FAQ Assistant Reasoning Engines, verifying:
  1. Sentiment Router classifies an urgent/frustrated message as `URGENT_FRUSTRATED`.
  2. FAQ Assistant answers an order status query (`#10001`) with a grounded response
     (`grounded == True`) and cleans up its test session via `:query` (`async_delete_session`).

Exits 0 on success, 1 on any failure (triggering automated rollback in CI).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.clients import agent_runtime  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ci.smoke_agents")


async def run_agent_smoke_tests(
    sentiment_url: str | None = None,
    faq_url: str | None = None,
) -> bool:
    """Executes live probes against both deployed Agent Runtime engines."""
    if sentiment_url:
        agent_runtime.SENTIMENT_AGENT_URL = sentiment_url
    if faq_url:
        agent_runtime.FAQ_AGENT_URL = faq_url

    try:
        # 1. Probe Sentiment Router Agent
        urgent_prompt = (
            "This is ridiculous, I was charged twice and want to cancel my account immediately!"
        )
        logger.info("Probing Sentiment Router at %s ...", agent_runtime.SENTIMENT_AGENT_URL)
        sentiment_res = await agent_runtime.call_sentiment_agent(urgent_prompt)
        label = sentiment_res.get("sentiment")
        if label != agent_runtime.SENTIMENT_URGENT:
            logger.error(
                "Sentiment probe failed: expected %r, got %r (full=%s)",
                agent_runtime.SENTIMENT_URGENT,
                label,
                sentiment_res,
            )
            return False
        logger.info("✅ Sentiment Router probe passed: sentiment=%s", label)

        # 2. Probe FAQ Assistant Agent
        ticket_id = str(uuid.uuid4())
        user_id = "ci_agent_smoke_probe"
        faq_prompt = "Can you check the status of order #10001?"
        logger.info("Probing FAQ Assistant at %s (ticket_id=%s) ...", agent_runtime.FAQ_AGENT_URL, ticket_id)
        try:
            faq_res = await agent_runtime.call_faq_agent(faq_prompt, session_id=ticket_id, user_id=user_id)
            answer = (faq_res.get("answer") or "").strip()
            grounded = bool(faq_res.get("grounded"))
            if not grounded or not answer or faq_res.get("escalate"):
                logger.error("FAQ Assistant probe failed: grounded=%s, res=%s", grounded, faq_res)
                return False
            logger.info(
                "✅ FAQ Assistant probe passed: grounded=%s, citations=%s, answer_preview=%r",
                grounded,
                faq_res.get("citations"),
                answer[:100],
            )
        finally:
            await agent_runtime.delete_faq_session(ticket_id, user_id)

        return True
    except Exception as exc:
        logger.error("Agent Runtime smoke test failed with exception: %s: %s", type(exc).__name__, exc)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test deployed Vertex AI Agent Runtime engines.")
    parser.add_argument("--sentiment-url", default=os.environ.get("SENTIMENT_AGENT_URL", ""))
    parser.add_argument("--faq-url", default=os.environ.get("FAQ_AGENT_URL", ""))
    args = parser.parse_args(argv)

    ok = asyncio.run(
        run_agent_smoke_tests(
            sentiment_url=args.sentiment_url or None,
            faq_url=args.faq_url or None,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
