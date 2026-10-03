import logging
import os

from pydantic_ai import Agent

from obot.config import Settings

log = logging.getLogger(__name__)


def setup_tracing(settings: Settings) -> None:
    """Send Pydantic AI traces (prompts, tokens, latency) to Langfuse if configured."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key.get_secret_value()
    os.environ["LANGFUSE_HOST"] = settings.langfuse_host

    from langfuse import get_client

    if not get_client().auth_check():
        log.warning("Langfuse credentials rejected; tracing disabled")
        return
    Agent.instrument_all()
    log.info("Langfuse tracing enabled")
