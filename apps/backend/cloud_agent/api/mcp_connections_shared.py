"""Shared MCP owner-API contracts and exact HTTP error translation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastapi import HTTPException

if TYPE_CHECKING:
    from cloud_agent.ports import ConversationQueries, McpConnectionRepository

    class _McpConnectionsStorage(Protocol):
        """The storage capabilities used by the MCP owner API."""

        mcp: McpConnectionRepository
        conversation_queries: ConversationQueries

else:

    class _McpConnectionsStorage(Protocol):
        """The storage capabilities used by the MCP owner API."""

        mcp: McpConnectionRepository
        conversation_queries: ConversationQueries


def _conflict(message: str) -> HTTPException:
    """A state change that lost a race with another writer."""
    return HTTPException(
        status_code=409,
        detail={"error": {"type": "conflict", "message": message}},
    )


def _not_found(message: str) -> HTTPException:
    """A resource that does not exist, or is not this user's."""
    return HTTPException(
        status_code=404,
        detail={"error": {"type": "not_found", "message": message}},
    )


def _bad_request(message: str) -> HTTPException:
    """A request whose input cannot be turned into a connection."""
    return HTTPException(
        status_code=400,
        detail={"error": {"type": "invalid_request_error", "message": message}},
    )


def _require_store(store: _McpConnectionsStorage | None) -> _McpConnectionsStorage:
    """Refuse a request that needs the database when there is none."""
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "server_error",
                    "message": "MCP needs a configured database.",
                }
            },
        )
    return store
