# 02 · Handlers stop crossing the seam

Status: ready-for-agent
Blocked by: 01
Commit: `refactor: route Telegram handlers through the intake module`

Spec decisions 7 and 11. Add `get_review`, `apply_quick_edit` and `refetch` to the intake module, and `refetch` to TriageWorkflow. `handlers.py` no longer touches `intake_service.store` or imports `fetch_url`.

Done when `grep -n "\.store\.\|fetch_url" src/backlog_tamer/integrations/telegram/handlers.py` finds nothing, the intake-flow tests cover `get_review` and `apply_quick_edit`, and the workflow tests cover refetch.
