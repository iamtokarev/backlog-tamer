# Backlog Tamer

Turns links and notes sent over Telegram into Projects in a Notion backlog, with a human approving every Draft before anything is written.

## Language

### Intake

**Intake**:
One captured item's journey from the message the user sent to a committed, rejected, or abandoned backlog entry.
_Avoid_: Capture job, request

**Draft**:
The proposed Project for one **Intake**, shown to the user for **Review**. It carries no Tasks.
_Avoid_: Proposal, suggestion

**Grounding**:
What was actually read from the source page behind a **Draft**, or why it could not be read.
_Avoid_: Fetch result, context

### Review

**Review**:
The human step between a Draft and Notion: the user approves, rejects, revises, or quick-edits the Draft.
_Avoid_: Confirmation step, approval flow

**Confirmation**:
The record of one Intake's Review and where it stands, from pending review through committed, rejected, failed, duplicate, or undone.
_Avoid_: Ticket, job

**Quick edit**:
A field the user changes on the **Draft** directly with a button. It holds for the rest of the **Review**, through every **Revision**.
_Avoid_: Manual override, patch

**Revision**:
Free-text feedback that sends the **Draft** back to have the fields it is about decided again.
_Avoid_: Edit, correction

### Triage

**Triage turn**:
One decision pass between two human decisions: the Draft is decided from an Intake, or decided again from a **Revision**.
_Avoid_: Step, invocation, agent run

**Label set**:
The fixed options a Draft is decided over: project types, intents and priorities, plus the tags already in the backlog.
_Avoid_: Taxonomy, categories

**Routing**:
Deciding which Draft fields a **Revision** is about, so only those fields are decided again.
_Avoid_: Intent detection, dispatch
