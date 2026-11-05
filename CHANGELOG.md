# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-05

### Added

- `buckets.strategy: board`: Planner buckets mirror the columns of a project
  board, in both directions. Moving a card moves the task; dragging a task
  moves the card, and adds the issue to the board if needed. Supported boards:
  GitHub Projects (single-select field, `Status` by default), Gitea project
  boards (Gitea 28 or newer) and GitLab issue boards (label lists).
- `bucket` field in `planner_to_git.fields`, which controls whether bucket moves in
  Planner are written back to Git.
- `git2planner validate` checks access to the project board.

### Changed

- Release workflow: can be started from the Actions tab, can be re-run for
  a published version, and uses the Node 24 releases of the GitHub actions.

## [1.0.0] - 2026-11-02

First stable release.

### Added

- Issue providers: GitHub and GitHub Enterprise Server, Gitea, Forgejo
  (including Codeberg), GitLab.com and self-managed GitLab (nested groups).
- Sync directions `git-to-planner`, `planner-to-git` and `both`. Each pair
  keeps a field-level baseline, so values written by git2planner are never
  taken for user edits, and a `conflicts` policy decides between the two sides.
- Planner → Git: state, title (template prefix stripped), assignees, labels,
  priority, bucket labels, due dates, checklist ticks and new items, and
  optionally the description. New Planner tasks can become issues
  (`create_issues`, optionally limited to some buckets).
- Label → Planner category mapping (auto, explicit, off) that names free
  slots automatically and picks the colour closest to the label's.
- Assignee resolution through explicit mappings, public Git e-mails and an
  e-mail domain, with group membership checks and fallback assignees.
- Attachments added as task links, or uploaded to the group's SharePoint
  library (including large files through upload sessions).
- Bucket strategies (fixed, per label, per milestone) with a "closed"
  bucket and automatic bucket creation.
- Checklists from Markdown task lists, priorities from labels, due dates
  from issues or milestones, configurable title template and description footer.
- Authentication with client secret, federated credentials, GitHub Actions
  OIDC, device code (delegated) or a ready-made access token.
- Incremental fetching, ETag-based updates with conflict retry, throttling
  and rate-limit handling, an atomic and locked state file, and re-linking of
  existing tasks when the state is lost.
- CLI commands `sync`, `validate`, `init`, `login`, `logout`, `plans` and
  `describe-plan`, with `--dry-run`, `--direction`, `--only`, `--issue`,
  `--full`, `--force`, `--reconcile`, `--watch` and `--json`.
- Docker image, composite GitHub Action, and examples for Gitea/Forgejo
  Actions, GitLab CI, cron and systemd.

[1.1.0]: https://github.com/francescodifilippo/git2planner/releases/tag/v1.1.0
[1.0.0]: https://github.com/francescodifilippo/git2planner/releases/tag/v1.0.0
