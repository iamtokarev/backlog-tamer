# Backlog Tamer

Turns links and notes sent over Telegram into Projects and Tasks in a Notion backlog, with a human approving every agent draft before anything is written.

## Language

### Intake

**Intake**:
One captured item's journey from the message the user sent to a committed, rejected, or abandoned backlog entry.
_Avoid_: Capture job, request

**Draft**:
The agent's proposed Project and its Tasks for one Intake, shown to the user for Review.
_Avoid_: Proposal, suggestion

**Grounding**:
What the agent actually read from the source page behind a Draft, or why it could not read it.
_Avoid_: Fetch result, context

### Review

**Review**:
The human step between a Draft and Notion: the user approves, rejects, revises, or quick-edits the Draft.
_Avoid_: Confirmation step, approval flow

**Confirmation**:
The record of one Intake's Review and where it stands, from pending review through committed, rejected, failed, duplicate, or undone.
_Avoid_: Ticket, job

**Quick edit**:
A field the user changes on the Draft directly with a button, without asking the agent to redraft. Later revisions must keep it.
_Avoid_: Manual override, patch

**Revision**:
Free-text feedback that sends the Draft back to the agent to redraft.
_Avoid_: Edit, correction

### Triage turns

**Triage turn**:
One run of the agent between two human decisions: from the message or feedback it receives to the next Turn outcome.
_Avoid_: Step, invocation

**Turn outcome**:
How a Triage turn ends: a Draft that needs Review, an approval, or a rejection. There are no other endings.
_Avoid_: Result, route

**Checkpoint**:
The point where a paused Triage waits on the human. The next Triage turn resumes from it.
_Avoid_: Interrupt, resume token, session
