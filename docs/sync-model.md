# Sync model

This page explains how git2planner decides what to write, and why it never
sends its own changes back.

## Pairs and baselines

Every synced issue is paired with exactly one Planner task. For each pair the
state file keeps a **baseline** of what was written last, expressed in
Planner terms:

| Field | Baseline |
|-------|----------|
| title | the task title as last written |
| state | 0 or 100 % |
| bucket | bucket id |
| priority | 0–10 |
| due date | ISO timestamp |
| assignees | Entra object ids that git2planner manages |
| labels | category slots that git2planner manages |
| description | SHA-256 of the description text |
| checklist | item id → ticked |
| links | link → alias |

## A run, step by step

1. **Fetch issues.** All open issues are read. Closed issues are only read
   when they changed since the previous run, unless `--full` is given or
   `filters.state` is `closed` or `all`. Tracked issues that vanished are
   looked up one by one to tell "closed" from "deleted or transferred".
2. **Pull** (`planner-to-git`, `both`). The plan's tasks are read in one
   paged call, plus their details in batches of 20 when checklists or
   descriptions are pulled. For each pair:
   - a field that differs from the baseline in Planner but not in Git is
     written to the issue, and the baseline is updated;
   - a field that differs on both sides is a **conflict**: `conflicts: git`
     skips it here (the push step then overwrites Planner), while
     `conflicts: planner` writes the Planner value to Git;
   - a task that no longer exists is marked as deleted (and the issue closed
     when `close_deleted` is set).

   With `create_issues: true`, tasks that are not linked to any issue become
   new issues, and their current values become the baseline.
3. **Push** (`git-to-planner`, `both`). For each issue, the task it should
   be is computed and compared with the baseline. Only differing fields are
   sent, each with the ETag that was stored, and stale ETags are refreshed
   and retried. New issues that match the filters get a task. If the state
   was lost, a task that already links to the issue is adopted instead of
   duplicated.
4. **Save** the state atomically.

## What is never sent back

- The title template (`#12 `) is stripped from titles coming from Planner.
- The `Synced from …` footer is stripped from descriptions.
- Links, uploaded files and the checklist ids created by git2planner are
  part of the baseline, so they are never seen as Planner edits.
- Assignees and labels that cannot be expressed in Git (for example a Planner
  user without a known Git login, or an unnamed label) stay in Planner only.
  They are neither pushed to Git nor removed from Planner.
- Planner priorities are compared by level (urgent, important, medium, low),
  and due dates by calendar day, so equivalent values do not ping-pong
  between the two sides.

## One-way sync and manual edits

With `direction: git-to-planner`, Git is the source of truth. An edit made in
Planner is kept until the same field changes in the issue. For example, a
task dragged to another bucket stays there until the issue's bucket label
changes, and a title edited in Planner stays until the issue is renamed. Use
`--force` to overwrite every synced field once.

## Concurrency

The state file is locked for the duration of a run. In CI, also serialise the
job (`concurrency:` in GitHub/Gitea/Forgejo Actions, `resource_group:` in
GitLab CI).
