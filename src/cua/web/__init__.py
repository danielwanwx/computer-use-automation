"""Thin HTTP entrypoint for the single in-process ApplicationService."""

from cua.web.app import create_app

__all__ = ["create_app"]

