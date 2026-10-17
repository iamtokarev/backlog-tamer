# 01 · TriageWorkflow behind one seam

Status: ready-for-agent
Commit: `refactor: put the ADK workflow behind TriageWorkflow` (the body notes the fix for "Approve" typed as revision feedback, or split it into a `fix:` commit)

Spec decisions 1–6, 9, 10. Add `TriageWorkflow`, the Turn outcome types and Checkpoint. Switch `start_intake` / `resume_intake` to it, with the intake module dispatching approve, reject and revise to named operations. Change `handle_human_review` to route on a structured reply. Move grounding and task-name composition behind the seam. Point the dev runner at TriageWorkflow.

Done when `tests/test_triage_workflow.py`, `tests/fakes.py` and `tests/test_intake_flow.py` (the start / revise / approve / reject / terminal cases) exist and pass, `test_draft_grounding.py` no longer uses `IntakeService.__new__`, and `make test lint format-check` is green.
