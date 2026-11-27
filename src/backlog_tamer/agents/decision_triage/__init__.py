"""Prototype: typed decision-model triage behind the TriageWorkflow seam.

Selected with TRIAGE__ENGINE=decision. Evaluation and rationale live in
.scratch/decision-triage-eval/RESULTS.md.
"""

from .engine import DecisionTriage

__all__ = ["DecisionTriage"]
