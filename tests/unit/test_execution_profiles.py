"""Replica-local shared-sandbox claim compatibility."""

from cloud_agent.execution_profiles import (
    compatible_shared_sandbox_followup_attested,
    shared_sandbox_claim_attested,
)
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)

TARGET = "target-sha"


def _attestation(
    *,
    helpers: dict[str, str] | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, object]:
    return {
        "sandbox_image_digest": "sha256:image",
        "runner_capabilities": (
            [
                SHARED_SANDBOX_CAPABILITY,
                NATIVE_FORK_AT_ANCHOR_CAPABILITY,
                shared_deployment_capability(TARGET),
            ]
            if capabilities is None
            else capabilities
        ),
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS) if helpers is None else helpers,
    }


def test_the_exact_image_may_continue_its_groups_and_create_new_ones() -> None:
    current = _attestation()

    assert compatible_shared_sandbox_followup_attested(current)
    assert shared_sandbox_claim_attested(current, expected_deployment_sha=TARGET)


def test_every_pinned_helper_is_required_for_a_follow_up_too() -> None:
    for helper_id in SHARED_SANDBOX_HELPER_PINS:
        missing = _attestation(
            helpers={k: v for k, v in SHARED_SANDBOX_HELPER_PINS.items() if k != helper_id}
        )

        assert not compatible_shared_sandbox_followup_attested(missing), helper_id
        assert not shared_sandbox_claim_attested(missing, expected_deployment_sha=TARGET)


def test_a_fork_helper_of_another_version_is_rejected_outright() -> None:
    """The anchor-taking fork helper is pinned exactly, like every other helper.

    An image whose ``claude-session-fork`` ignores the anchor would fork the
    latest message; nothing else in the attestation tells that image apart.
    """
    other_fork_helper = _attestation(
        helpers={**SHARED_SANDBOX_HELPER_PINS, "claude-session-fork": "1"}
    )

    assert not compatible_shared_sandbox_followup_attested(other_fork_helper)
    assert not shared_sandbox_claim_attested(other_fork_helper, expected_deployment_sha=TARGET)


def test_the_previous_generation_may_only_continue_the_groups_it_owns() -> None:
    """A rolling deployment: the same image contract, not yet the target deployment."""
    previous = _attestation(
        capabilities=[
            SHARED_SANDBOX_CAPABILITY,
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability("previous-sha"),
        ]
    )

    assert compatible_shared_sandbox_followup_attested(previous)
    assert not shared_sandbox_claim_attested(previous, expected_deployment_sha=TARGET)


def test_new_work_needs_the_at_anchor_fork_capability() -> None:
    no_fork = _attestation(
        capabilities=[SHARED_SANDBOX_CAPABILITY, shared_deployment_capability(TARGET)]
    )

    assert compatible_shared_sandbox_followup_attested(no_fork)
    assert not shared_sandbox_claim_attested(no_fork, expected_deployment_sha=TARGET)
