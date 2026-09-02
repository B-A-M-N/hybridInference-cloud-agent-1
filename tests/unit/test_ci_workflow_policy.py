"""Two workflow properties a public repository cannot afford to lose.

Both are one edit away from silently disappearing — a `runs-on` pasted from an
older job, a target step simplified "for readability" — and neither failure
shows in CI, because CI is exactly what they govern. So they are pinned here,
where a change to the workflows has to change a test.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def test_every_ci_job_a_pull_request_can_reach_runs_hosted():
    """A pull_request carries code nobody here has vetted.

    On a public repository that includes forks. A self-hosted runner is a
    machine of ours, so every job the ``pull_request`` event can reach must
    pick a GitHub-hosted runner for that event and keep the self-hosted pool
    for trusted ones. The conditional form is the only shape that does both.
    """
    offenders = []
    for job, spec in _jobs("ci.yml").items():
        runs_on = spec.get("runs-on")
        bare_pool = isinstance(runs_on, list) and "self-hosted" in runs_on
        unconditional = (
            isinstance(runs_on, str)
            and "self-hosted" in runs_on
            and "github.event_name == 'pull_request'" not in runs_on
        )
        if bare_pool or unconditional:
            offenders.append(job)
    assert not offenders, (
        f"these jobs would run untrusted pull_request code on a self-hosted runner: {offenders}"
    )


def test_staging_deploys_refuse_anything_not_on_dev():
    """The dispatch form defaults to ``main``, and main lags dev by weeks.

    A dispatch left on that default deployed main to staging on 2026-09-02.
    Both deploy jobs resolve their own target commit, so both must carry the
    ancestor check — one guarded job and one unguarded job is how half a
    deployment gets downgraded.
    """
    unguarded = []
    for job, spec in _jobs("deploy-staging.yml").items():
        steps = spec.get("steps", [])
        target = [s for s in steps if s.get("id") == "target"]
        if not target:
            continue
        live = [
            line.strip()
            for line in (target[0].get("run") or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        # A live `if ! git merge-base --is-ancestor ...` followed by an exit:
        # the substring alone would also match a commented-out guard.
        guarded = any(
            line.startswith("if ! git merge-base --is-ancestor") for line in live
        ) and any(line == "exit 1" for line in live)
        if not guarded:
            unguarded.append(job)
    assert not unguarded, f"these deploy jobs would deploy a commit that is not on dev: {unguarded}"
    assert (
        sum(
            1
            for s in _jobs("deploy-staging.yml").values()
            if any(st.get("id") == "target" for st in s.get("steps", []))
        )
        == 2
    )
