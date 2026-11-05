# git2planner

[![CI](https://github.com/francescodifilippo/git2planner/actions/workflows/ci.yml/badge.svg)](https://github.com/francescodifilippo/git2planner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

Sync Git issues to Microsoft Planner tasks, and back, with automatic category
mapping, assignee resolution and attachment handling. Built for teams that track
work in **GitHub, Gitea, Forgejo or GitLab** and plan it in **Microsoft 365**.

```
 GitHub / GitHub Enterprise ┐                        ┌ tasks, buckets, labels
 Gitea / Forgejo / Codeberg ├──  git2planner sync  ──┤ assignees, checklists
 GitLab.com / self-managed  ┘   (one way or both)    └ links and files
```

## Features

- **Four providers**: GitHub (and GitHub Enterprise Server), Gitea, Forgejo
  (including Codeberg) and GitLab (including nested groups and self-managed
  instances).
- **One way or both ways**: `git-to-planner`, `planner-to-git` or `both`.
  Pick it per repository or for a single run with `--direction`.
- **Edits stay where they were made**: git2planner remembers what it last
  wrote on each side and compares every field with that baseline. Only real
  edits cross over, so values it wrote itself never come back as changes. When
  both sides edit the same field, a `conflicts` policy decides which one wins.
- **Category mapping**: labels become Planner labels (`category1`–`category25`).
  Missing labels are created on the fly, using the free slot whose colour is
  closest to the Git label's colour. Explicit mappings and exclusions are also
  supported.
- **Assignee resolution**: explicit mapping, public Git e-mail, or
  `<login>@your-domain`. Results are checked against the plan's Microsoft 365
  group, with optional fallback assignees.
- **Attachments**: the issue and the files uploaded to it are added as task
  links. Optionally the files are copied into the group's SharePoint document
  library.
- **Buckets**: one fixed bucket, a bucket per label or per milestone, plus a
  "closed" bucket. Missing buckets are created automatically.
- **Rich tasks**: title template, plain-text description with a link back to
  the issue, Markdown task lists turned into Planner checklists, priority from
  labels, due date from the issue or its milestone.
- **Safe to run anywhere**: dry run, incremental fetching, ETag handling,
  throttling and retry support, an atomic state file with locking, and
  re-linking of existing tasks when the state file is lost.
- **Runs where you already work**: CLI, Docker image, GitHub Action, and
  examples for Gitea/Forgejo Actions and GitLab CI.

## Quick start

```bash
pip install git2planner
# or straight from a release tag
pip install "git2planner @ git+https://github.com/francescodifilippo/git2planner@v1.0.0"
```

1. **Register an app in Microsoft Entra ID** and grant it Planner permissions.
   [docs/azure-setup.md](docs/azure-setup.md) walks through it in five minutes.
2. **Create a configuration file** and edit it:

   ```bash
   git2planner init --provider github      # or gitea, forgejo, gitlab
   ```

3. **Provide the secrets** as environment variables:

   ```bash
   export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
   export GITHUB_TOKEN=...                 # GITEA_TOKEN, FORGEJO_TOKEN or GITLAB_TOKEN
   ```

4. **Find your plan id**, check the setup, preview, then run:

   ```bash
   git2planner plans --group <microsoft-365-group-id>
   git2planner validate
   git2planner sync --dry-run
   git2planner sync
   ```

## Configuration at a glance

```yaml
graph:
  auth: auto                      # client_secret, federated, github_oidc, device_code, access_token

defaults:
  direction: both                 # git-to-planner | planner-to-git | both
  conflicts: git                  # who wins when both sides changed the same field
  buckets: {default: To do, closed: Done}
  categories: {mode: auto, exclude: [priority/high]}
  priority: {mapping: {priority/high: important}}
  assignees: {email_domain: contoso.com, mapping: {octocat: mona@contoso.com}}
  attachments: {mode: upload}

syncs:
  - name: backend
    source: {provider: github, repository: acme/backend}
    plan_id: xqQg5FS2LkCp935s-FIFm2QAFkHM
  - name: website
    source: {provider: gitlab, repository: acme/web/site}
    plan_id: xqQg5FS2LkCp935s-FIFm2QAFkHM
    filters: {labels: [planner]}
  - name: infra
    source: {provider: forgejo, url: https://codeberg.org, repository: acme/infra}
    plan_id: 9rRG4mjJS0ac3C6G2Qex-WQAEXX1
    planner_to_git: {create_issues: true, buckets: [Inbox]}
```

Every option is described in [docs/configuration.md](docs/configuration.md).

## What gets synchronised

| Git issue                     | Planner task                          | Back to Git (`planner-to-git`)             |
|-------------------------------|---------------------------------------|--------------------------------------------|
| Title                         | Title (`title_template`)              | Title, with the template prefix removed    |
| Open / closed                 | 0 % / 100 % complete, "closed" bucket | Ticking a task or moving it to "closed" closes the issue |
| Labels                        | Planner labels (categories)           | Added/removed labels                       |
| Priority labels               | Priority (urgent, important, …)       | Priority label                             |
| Label / milestone             | Bucket                                | Bucket label (`label` strategy)            |
| Assignees                     | Assignments                           | Assignees                                  |
| Due date / milestone due date | Due date                              | Due date (Gitea, Forgejo, GitLab)          |
| Body                          | Description (plain text + link)       | Optional (`fields: [description]`)         |
| `- [ ]` task list             | Checklist                             | Ticks and new checklist items              |
| Issue URL, uploaded files     | Links (references), SharePoint files  | –                                          |
| –                             | New task (`create_issues: true`)      | New issue                                  |

## How two-way sync works

For every issue–task pair, git2planner stores a **baseline**: the values it
last wrote, in Planner terms. On each run it compares both sides with that
baseline, field by field:

| Changed in Git | Changed in Planner | Result                                           |
|:--------------:|:------------------:|--------------------------------------------------|
| no             | no                 | nothing is sent                                  |
| yes            | no                 | Planner is updated                               |
| no             | yes                | the issue is updated                             |
| yes            | yes                | the `conflicts` side wins (`git` by default)     |

With `direction: both`, each run first pulls Planner edits into Git, then
pushes Git changes into Planner. Because of the baseline, the title prefix,
the description footer and the links that git2planner adds to a task are
never sent back to Git as if they were user edits. Edits made in Planner to
fields that are synced one way only are kept until the issue itself changes.

If you would rather alternate, keep `direction: git-to-planner` in the
configuration and run the other direction when you need it:

```bash
git2planner sync --direction planner-to-git
```

More details are in [docs/sync-model.md](docs/sync-model.md).

## Running on a schedule

### GitHub Actions

```yaml
on:
  schedule: [{cron: "*/30 * * * *"}]
  issues:
  workflow_dispatch:

permissions:
  issues: write        # read is enough for git-to-planner
  id-token: write      # only for graph.auth: github_oidc

jobs:
  sync:
    runs-on: ubuntu-latest
    concurrency: git2planner
    steps:
      - uses: actions/checkout@v5
      - uses: actions/cache@v5
        with:
          path: .git2planner
          key: git2planner-${{ github.run_id }}
          restore-keys: git2planner-
      - uses: francescodifilippo/git2planner@v1
        env:
          AZURE_TENANT_ID: ${{ vars.AZURE_TENANT_ID }}
          AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}
          AZURE_CLIENT_SECRET: ${{ secrets.AZURE_CLIENT_SECRET }}
          GITHUB_TOKEN: ${{ github.token }}
```

The cache keeps the state file between runs. Without it git2planner still
works: it re-links existing tasks through their issue links. It just cannot
tell Planner edits apart from its own writes, so Git wins on every field.
More examples, for Gitea/Forgejo Actions, GitLab CI, Docker and cron, are in
[examples/](examples).

### Docker

```bash
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" \
  -e AZURE_TENANT_ID -e AZURE_CLIENT_ID -e AZURE_CLIENT_SECRET -e GITHUB_TOKEN \
  ghcr.io/francescodifilippo/git2planner:1 sync
```

## Command line

| Command                          | Purpose                                                    |
|----------------------------------|------------------------------------------------------------|
| `git2planner init`               | write an annotated configuration file                      |
| `git2planner validate`           | check the configuration, credentials and permissions       |
| `git2planner sync`               | run the configured syncs                                   |
| `git2planner plans --group ID`   | list the plans of a Microsoft 365 group                    |
| `git2planner describe-plan ID`   | show the buckets and labels of a plan                      |
| `git2planner login` / `logout`   | device-code sign-in for delegated permissions              |

Useful `sync` options: `--dry-run`, `--direction`, `--only NAME`,
`--issue NUMBER`, `--full` (re-read all closed issues), `--force` (rewrite
every synced field), `--reconcile` (link existing tasks), `--watch SECONDS`,
`--json`.

Exit codes: `0` success, `1` some items failed, `2` configuration error,
`3` authentication error.

## Permissions

| Purpose                       | Application permission (client secret / federated) | Delegated (device code) |
|-------------------------------|------------------------------------------|-------------------------|
| Read and write Planner        | `Tasks.ReadWrite.All`                    | `Tasks.ReadWrite`       |
| Verify group membership       | `GroupMember.Read.All`                   | `GroupMember.Read.All`  |
| Resolve users                 | `User.Read.All`                          | `User.ReadBasic.All`    |
| Upload attachments (optional) | `Sites.ReadWrite.All`                    | `Files.ReadWrite.All`   |

Git tokens need read access to issues, plus write access for `planner-to-git`:

- **GitHub**: a fine-grained token with *Issues* (read/write) and *Metadata* (read), or `github.token` in Actions.
- **Gitea / Forgejo**: `read:issue` (or `write:issue`), `read:repository` and `read:user`.
- **GitLab**: `read_api` (or `api` for writes).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [changelog](CHANGELOG.md).

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
