"""Which models a user's agent jobs may call — asked, no longer computed.

The source walked the gateway's own route registry: published flag, required
role, the visibility resolver's override, disabled-for-user preferences, chat
versus embedding. None of that data exists in this service, so this module
keeps its two functions and its callers, and gets the answer over HTTP.

**The signatures changed and could not not change.** They took a
``router_exec`` and a ``visibility_resolver`` — objects that were the registry.
Keeping them as parameters would have meant either passing ``None`` everywhere
or inventing a local stand-in for a decision the gateway owns; both are worse
than a call site that says what it now does.

**Failure is closed.** A gateway that cannot be reached yields *no* models, not
every model. Getting this backwards would let an outage widen what a job may
call, at exactly the moment nobody is watching.
"""

from __future__ import annotations

from cloud_agent.gateway import GatewayClient, GatewayError
from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)


async def agent_visible_models(gateway: GatewayClient, *, user_id: str) -> list[str]:
    """Return the canonical chat-model ids this user's agent jobs can call.

    Args:
        gateway: Client for the gateway's internal contracts.
        user_id: The gateway's user identifier.

    Returns:
        Model ids in the gateway's registry order, or an empty list if the
        catalog cannot be read.
    """
    try:
        return await gateway.model_catalog(user_id)
    except GatewayError:
        # Empty, never "everything". A picker with no options is visibly
        # broken; a picker showing models the user cannot call produces a job
        # that dies at its first request, far from the cause.
        logger.warning(
            "agent_model_catalog_unavailable",
            exc_info=True,
            extra={"event": "agent_model_catalog_unavailable", "user_id": user_id},
        )
        return []


async def agent_model_resolvable(model: str, gateway: GatewayClient, *, user_id: str) -> bool:
    """Whether one requested model would resolve for this user.

    The create-time fail-fast the source had, with the same purpose: rejecting
    an unknown model costs a 400, while accepting it costs a claim, a clone and
    an attempt before the first model call dies.

    One behavioural difference, and it is not avoidable here: the source
    resolved **aliases** because it held the registry, so a job could be created
    naming an alias. This compares against canonical ids, so an alias now reads
    as unresolvable. The gateway is where alias resolution lives; teaching the
    catalog endpoint to return aliases is the fix, and it is a change to that
    contract rather than something this module can decide.

    Args:
        model: The requested model id.
        gateway: Client for the gateway's internal contracts.
        user_id: The gateway's user identifier.

    Returns:
        Whether the model is in this user's catalog.
    """
    return model in await agent_visible_models(gateway, user_id=user_id)
