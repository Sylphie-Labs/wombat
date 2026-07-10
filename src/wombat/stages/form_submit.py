"""wombat.stages.form_submit — FormSubmitStage: journal ONE proposed ``submit_form`` action,
park ``AwaitHuman``, then let the shared ``DispatchApprovedStage`` dispatch exactly ONE approved
``submit_form`` call (TK-135, Q-114).

A ``ProposeDispatchStage`` (``wombat.stages.dispatch_base``, TK-149/Q-91) subclass —
``build_proposal`` is the only method this stage implements; ``run()`` itself is inherited and
NON-overridable, so the journal-before-park ordering can never be skipped or reordered here. The
dispatch side is the EXISTING generic ``DispatchApprovedStage``
(``wombat.stages.dispatch_approved``) — ZERO new dispatch-side code (Q-114(d)): it already binds
``EXTERNAL_DISPATCH_POLICY`` and reads the approve/reject decision via ``ctx.read_human_input``.
Wiring the two together into a graph (``propose_stage_name="form_submit"``,
``dispatch_stage_name="form_dispatch"``, ``capability="browser"``) is a caller's job — there is no
v1 pathway that registers form submit yet (recorded posture; runtime/bootstrap wiring is out of
scope for this ticket).

Upstream contract: an artifact of kind ``FORM_SUBMIT_REQUEST`` (``wombat.form_submit_request``)
with data ``{"url": str, "fields": [{"role": str, "name": str, "value": str}, ...],
"submit": {"role": str, "name": str}}`` — the EXACT ``submit_form`` capability args shape
(``wombat.capabilities.playwright_capability.BROWSER_INPUT_SCHEMA``, Q-114). ``dispatch_args``
carries that shape through UNCHANGED (plus the fixed ``"action": "submit_form"``), so the caller's
``DispatchApprovedStage`` needs only ``args_from_artifact=lambda art: dict(art.data)`` — the
identity mapping. A malformed upstream payload (missing/mistyped ``url``/``fields``/``submit``)
raises a loud ``ValueError`` here, never a silent best-effort parse: the human approves exactly
what this stage read off the artifact, so a malformed read must never quietly substitute
something else.

``human_summary`` enumerates the exact target URL and EVERY field name+value verbatim (Q-114): the
human approves exactly what dispatches — no summarization or truncation that could hide a field
from the reviewer.

Q-113(c)/Q-114(d) — one gated dispatch is the whole unit of work: the first external dispatch on a
drive taints it, so any follow-up dispatch attempt on the same drive is dead after the taint latch
(see ``PlaywrightCapability``'s own module docstring). This stage does not, and structurally
cannot, attempt more than the one dispatch ``DispatchApprovedStage`` performs.
"""

from __future__ import annotations

from typing import Any

from cogworx.loop.stage import StageContext

from wombat.stages.dispatch_base import ProposalWriter, ProposedAction, ProposeDispatchStage
from wombat.trail.schema import ActionType

# The upstream artifact kind this stage's build_proposal reads (Q-114).
FORM_SUBMIT_REQUEST = "wombat.form_submit_request"


def _field_args(raw: Any) -> list[dict[str, str]]:
    """Validate+normalize the upstream ``fields`` payload — a non-empty list of
    ``{role, name, value}`` objects. Raises ``ValueError`` loudly on anything else."""
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"form_submit: 'fields' must be a non-empty list, got {raw!r}")
    fields: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"form_submit: each field must be an object, got {entry!r}")
        try:
            role, name, value = entry["role"], entry["name"], entry["value"]
        except KeyError as exc:
            raise ValueError(f"form_submit: field missing {exc.args[0]!r}: {entry!r}") from exc
        fields.append({"role": str(role), "name": str(name), "value": str(value)})
    return fields


def _submit_args(raw: Any) -> dict[str, str]:
    """Validate+normalize the upstream ``submit`` payload — a ``{role, name}`` object. Raises
    ``ValueError`` loudly on anything else."""
    if not isinstance(raw, dict):
        raise ValueError(f"form_submit: 'submit' must be an object, got {raw!r}")
    try:
        role, name = raw["role"], raw["name"]
    except KeyError as exc:
        raise ValueError(f"form_submit: submit missing {exc.args[0]!r}: {raw!r}") from exc
    return {"role": str(role), "name": str(name)}


class FormSubmitStage(ProposeDispatchStage):
    """Journals ONE proposed ``submit_form`` action and parks ``AwaitHuman`` (TK-135, Q-114).

    ``run()`` is inherited from ``ProposeDispatchStage`` and non-overridable; this class supplies
    only ``build_proposal``.
    """

    name = "form_submit"

    def __init__(self, *, writer: ProposalWriter, upstream_stage_name: str) -> None:
        super().__init__(
            writer=writer,
            dispatch_stage_name="form_dispatch",
            action_type=ActionType.FORM_SUBMIT,
        )
        self._upstream_stage_name = upstream_stage_name

    async def build_proposal(self, ctx: StageContext) -> ProposedAction:
        art = await ctx.last_output(self._upstream_stage_name)
        if art is None:
            msg = f"{self.name}: no {self._upstream_stage_name!r} output available yet"
            raise ValueError(msg)
        data = art.data
        if not isinstance(data, dict) or "url" not in data:
            raise ValueError(
                f"{self.name}: malformed {FORM_SUBMIT_REQUEST!r} payload — missing 'url': "
                f"{data!r}"
            )
        url = str(data["url"])
        fields = _field_args(data.get("fields"))
        submit = _submit_args(data.get("submit"))

        field_lines = "\n".join(
            f"  - {field['name']} ({field['role']}) = {field['value']!r}" for field in fields
        )
        human_summary = (
            f"Submit the form at {url}?\n"
            f"Fields:\n{field_lines}\n"
            f"Submit control: {submit['name']} ({submit['role']})"
        )

        return ProposedAction(
            human_summary=human_summary,
            target=url,
            dispatch_args={
                "action": "submit_form",
                "url": url,
                "fields": fields,
                "submit": submit,
            },
        )


__all__ = ["FORM_SUBMIT_REQUEST", "FormSubmitStage"]
