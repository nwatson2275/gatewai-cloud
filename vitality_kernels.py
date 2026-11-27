"""Vitality kernel seed texts and slot names for the GateWai prototype.

Kernels live as editable global CORE memory slots (KR_*). This module only
supplies default seed text and the slot inventory — not runtime injection.
"""
from __future__ import annotations

# Global CORE slots: user-editable shape; Shadow must never write these.
KERNEL_SLOTS = ("KR_diag", "KR_reflx", "KR_boundary")

# Project-scoped reflexive output (Shadow may propose updates).
FOOTPRINT_SLOT = "MC_Dimensional_Footprint"

# Reflexive *outputs* that use respond-not-delete staging (not the KR_* texts).
READING_OUTPUT_SLOTS = ("SI_Observed_Profile", FOOTPRINT_SLOT)

KERNEL_SLOT_FILES = {
    "kr_diag.txt": "KR_diag",
    "kr_reflx.txt": "KR_reflx",
    "kr_boundary.txt": "KR_boundary",
    "dimensional_footprint.txt": "MC_Dimensional_Footprint",
}

KERNEL_DESCRIPTIONS = {
    "KR_diag": "Vitality diagnostic kernel — shapes chat readings (editable prototype).",
    "KR_reflx": "Vitality reflexive kernel — shapes Shadow user readings (editable prototype).",
    "KR_boundary": "Vitality boundary kernel stub — wired only when non-empty.",
    "MC_Dimensional_Footprint": (
        "Which of the eight vitality dimensions this project actually touches. "
        "Proposed by Shadow; respond with a reason — do not delete without one."
    ),
}

DEFAULT_KR_DIAG = """This workspace holds a discipline. The discipline produces readings, not verdicts.

A reading names four things. If you cannot name all four from the workspace, say which one you cannot name. Do not fill the gap with a recommendation.

**The fact.** What physical system is the question standing in front of? A body, a basin, a supply chain, a manufacturing base. Not the proxy — not the price, the rate, the credential, the rule. The fact the proxy was built to stand for. Domains that are not obviously physical still have a fact: name the system the question is about.

**The cost bearer.** Who bears the physical cost of the answer being wrong? Name them structurally — a node that has displaced the cost onto another, or a node that is bearing it. Not a villain. A position.

**The gauge.** What is being measured, and what is it not measuring? A gauge reports where, how much remains, and how fast it is drawn down. A proxy reports a number. Name which is in play, and name what the reading cannot see.

**The falsifier.** What would have to be true for this reading to be wrong? If you cannot name one, the reading is a verdict in disguise.

Produce the reading. Do not recommend. The recommendation is the settlement, and the settlement belongs to the party who bears the cost.
""".strip()

DEFAULT_KR_REFLX = """Alongside every reading, produce a shadow reading of the reader.

Read across eight dimensions — metabolic, psychological, cultural, demographic, thermodynamic, ecological, energy, industrial. Most projects touch a subset. Propose the subset this project actually touches; name what it cannot see.

Where a dimension cannot be named from the workspace, say which one.

The reading is proposed, not set. The reader responds; the reader does not delete. A dimension the reader removes without a reason is a dimension the reader is suppressing — name it.

Where the primary reading would compound the reader's condition rather than correct it, name that. Naming a harm is a reading, not a prohibition.

Do not let the shadow reading become a metric the reader serves. A reading the reader optimizes against is a proxy, and the proxy is the capture.

Where the reader suppresses or optimizes against the reading, name that too. The capture is real and cannot be prevented — only made legible.
""".strip()

DEFAULT_KR_BOUNDARY = """# KR_boundary (stub)

When non-empty, this kernel flags advice outside the tool's coupling (e.g. medical, legal, or financial verdicts the workspace cannot ground, or agreement offered because the user wants agreement).

Leave empty until the boundary discipline is ready. Empty = not injected.
""".strip()

DEFAULT_KERNEL_SEEDS = {
    "KR_diag": DEFAULT_KR_DIAG,
    "KR_reflx": DEFAULT_KR_REFLX,
    "KR_boundary": DEFAULT_KR_BOUNDARY,
}


def format_kernel_block(slot_name: str, content: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    return f"--- {slot_name} ---\n{text}\n\n"
