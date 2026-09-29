"""Per-request context propagated via contextvars.

Contextvars are the only "global" state the app keeps, and they are scoped to a
single request/task -- the process itself stays stateless, which is what lets us
run N interchangeable API replicas behind Nginx.
"""

from __future__ import annotations

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
route_var: ContextVar[str | None] = ContextVar("route", default=None)


def get_request_id() -> str | None:
    return request_id_var.get()


def get_route() -> str | None:
    return route_var.get()
