"""Select the US execution provider without importing broker credentials."""
import os


def us_broker() -> str:
    provider = os.getenv("PRISM_US_BROKER", "kis").strip().lower()
    if provider not in {"kis", "moomoo"}:
        raise ValueError("PRISM_US_BROKER must be kis or moomoo")
    return provider


def moomoo_accounts() -> list[dict]:
    from prism_core.moomoo_trading import MoomooConfig
    return [MoomooConfig.from_env().account()]
