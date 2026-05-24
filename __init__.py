from __future__ import annotations

try:
    from .hermes_auto_continue import register
except ImportError:  # pragma: no cover - plain pytest import fallback
    from hermes_auto_continue import register

__all__ = ["register"]
