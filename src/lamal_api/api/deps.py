"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.engine import Connection

from ..config import Settings


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


def get_connection(request: Request) -> Iterator[Connection]:
    """A read-only connection, closed when the request finishes."""
    engine: Engine = request.app.state.engine
    with engine.connect() as conn:
        yield conn


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


ConnectionDep = Annotated[Connection, Depends(get_connection)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
