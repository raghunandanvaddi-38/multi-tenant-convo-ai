"""AI gateway — the single LLM entry point for the application."""

from app.gateway.ai_gateway import AIGateway, get_gateway
from app.gateway.router import ProviderRouter

__all__ = ["AIGateway", "ProviderRouter", "get_gateway"]
