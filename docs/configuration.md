# Configuration reference

git2planner reads a YAML file, `git2planner.yml` by default. You can pass
another file with `-c path` or the `GIT2PLANNER_CONFIG` environment variable.
`git2planner init` writes an annotated starting point.

Any string value may reference environment variables: `${NAME}` fails when
`NAME` is not set, while `${NAME:-default}` falls back to `default`. Unknown
keys are rejected, so typos do not go unnoticed.

```yaml
graph: {...}       # Microsoft 365 connection
state: {...}       # where the issue <-> task mapping is stored
defaults: {...}    # settings merged into every sync
syncs: [...]       # one entry per repository -> plan pair
```

## `graph`

| Key | Default | Description |
|-----|---------|-------------|
| `auth` | `auto` | `client_secret`, `federated`, `github_oidc`, `device_code`, `access_token`, or `auto`. `auto` picks `access_token` if a token is set, then `client_secret` if a secret is set, then `federated` if an assertion file is set, and otherwise `device_code`. |
| `tenant_id` | `$AZURE_TENANT_ID` | Directory (tenant) id. Device code defaults to `organizations`. |
| `client_id` | `$AZURE_CLIENT_ID` | Application (client) id. |
| `client_secret` | `$AZURE_CLIENT_SECRET` | Secret for `client_secret`. |
| `client_assertion_file` | `$AZURE_FEDERATED_TOKEN_FILE` | File holding a federated token (Kubernetes workload identity, …) for `federated`. |
| `access_token` | `$GRAPH_ACCESS_TOKEN` | A ready-made Graph token, e.g. from `az account get-access-token --resource-type ms-graph`. |
| `token_cache` | `~/.cache/git2planner/token.json` | Refresh-token cache for `device_code` (created with mode 0600). |
| `scopes` | Planner, group, user and file scopes | Delegated scopes requested by `device_code`. |
| `authority` | `https://login.microsoftonline.com` | Use `https://login.microsoftonline.us` etc. for national clouds. |
| `graph_url` | `https://graph.microsoft.com/v1.0` | Graph endpoint (national clouds). |
| `timeout` | `30` | HTTP timeout in seconds. |

`github_oidc` exchanges the GitHub Actions OIDC token for a Graph token, so no
secret is needed. It requires `permissions: id-token: write` and a federated
credential on the app registration (see [azure-setup.md](azure-setup.md)).

## `state`

| Key | Default | Description |
|-----|---------|-------------|
| `path` | `.git2planner/state.json` | Relative paths are resolved from the configuration file's folder. |
| `enabled` | `true` | `false` keeps the state in memory. Every run then re-links tasks through their issue links, and Git wins on every field. |

The state file stores, for each issue, the task id, the ETags and the
baseline values last synced. It is written atomically and locked during a
run, so two runs cannot interleave. It contains no secrets.

## `defaults`

Any key that is valid inside a sync, except `name`, `source` and `plan_id`
which must stay per sync, can be placed under `defaults`. Mappings are merged
recursively, and lists or scalar values defined in a sync replace the default.

## `syncs[]`

| Key | Default | Description |
|-----|---------|-------------|
| `name` | the repository | Unique name, used by `--only` and in the state file. |
| `source` | – | The Git repository (see below). |
| `plan_id` | – | Planner plan id (`git2planner plans --group <id>`). |
| `direction` | `git-to-planner` | `git-to-planner`, `planner-to-git` or `both` (aliases: `push`, `pull`, `bidirectional`). |
| `conflicts` | `git` | Which side wins when a field changed in both places since the last sync: `git` or `planner`. |
| `on_close` | `complete` | What happens to the task when its issue is closed: `complete` (100 %, move to `buckets.closed`), `delete`, or `ignore` (stop updating it). |
| `on_missing` | `keep` | When an issue disappears (deleted or transferred): `keep`, `complete` or `delete` the task. |
| `recreate_deleted` | `false` | Recreate tasks that were deleted in Planner. |
| `filters`, `buckets`, `categories`, `assignees`, `priority`, `attachments`, `task`, `planner_to_git` | | See below. |

### `source`

| Key | Default | Description |
|-----|---------|-------------|
| `provider` | – | `github`, `gitea`, `forgejo` or `gitlab`. |
| `repository` | – | `owner/name`. GitLab accepts `group/subgroup/project`. |
| `url` | provider default | GitHub: `https://github.example.com` for Enterprise Server. Gitea/Forgejo: required (e.g. `https://codeberg.org`). GitLab: `https://gitlab.example.com` for self-managed. |
| `token` | `$GITHUB_TOKEN`/`$GH_TOKEN`, `$GITEA_TOKEN`, `$FORGEJO_TOKEN`/`$GITEA_TOKEN`, `$GITLAB_TOKEN` | API token. |
| `verify_ssl` | `true` | `false`, or the path of a CA bundle for self-signed certificates. |
| `timeout` | `30` | HTTP timeout in seconds. |

### `filters`

Filters decide which issues **get a new task**. An issue that already has a
task keeps being synced even if it stops matching, for example when a label
is removed.

| Key | Default | Description |
|-----|---------|-------------|
| `state` | `open` | `open`, `closed` or `all`. |
| `labels` | `[]` | The issue must carry all of these labels. |
| `exclude_labels` | `[]` | Issues with any of these labels are skipped. |
| `milestones` | `[]` | Only issues in one of these milestones. |
| `created_after` | – | `YYYY-MM-DD`. |
| `include_pull_requests` | `false` | Also sync pull requests (GitHub, Gitea, Forgejo). |

### `buckets`

| Key | Default | Description |
|-----|---------|-------------|
| `strategy` | `fixed` | `fixed`: always `default`. `label`: the first label listed in `mapping` that the issue has. `milestone`: a bucket named after the milestone, or `mapping[milestone]`. |
| `default` | `To do` | Bucket used when nothing else applies. |
| `mapping` | `{}` | `label: bucket` or `milestone: bucket`. |
| `closed` | – | Bucket for closed issues. In two-way mode, moving a task into this bucket closes the issue. |
| `create_missing` | `true` | Create buckets that do not exist yet. |

A task is only moved when the bucket computed from its issue changes, so
tasks dragged around in Planner stay where they were put.

### `categories`

| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `auto` | `auto`: every label becomes a Planner label with the same name. `explicit`: only labels in `mapping`. `off`. |
| `mapping` | `{}` | `label: Planner label name` or `label: categoryN` (1–25). |
| `exclude` | `[]` | Labels never turned into categories (e.g. priority labels). |
| `create_missing` | `true` | Name unused category slots after new labels. The slot whose colour is closest to the label colour is used. |

Planner has 25 label slots per plan. When they are all named, new labels
are skipped with a warning.

### `assignees`

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Sync assignees at all. |
| `mapping` | `{}` | `git-login: UPN, e-mail or Entra object id`. |
| `use_git_email` | `true` | Look up the user's public Git e-mail (no-reply addresses are ignored). |
| `email_domain` | – | Fallback: `<git-login>@<email_domain>`. It is also used to map Planner users back to Git logins. |
| `require_group_membership` | `true` | Skip users who are not members of the plan's group, which Planner would reject. |
| `fallback` | `[]` | Users assigned when none of the issue's assignees could be resolved. |

Resolution order: `mapping`, then the Git e-mail, then `email_domain`.
Planner allows at most 20 assignees per task.

### `priority`

| Key | Default | Description |
|-----|---------|-------------|
| `mapping` | `{}` | `label: urgent | important | medium | low | 0-10`. If several labels match, the most urgent wins. |
| `default` | `medium` | Priority when no label matches. |

### `attachments`

| Key | Default | Description |
|-----|---------|-------------|
| `mode` | `link` | `link`: add uploaded files as task links. `upload`: copy them into the group's document library and link the copy. `off`: only link the issue. |
| `folder` | `git2planner/{owner}/{repo}/{number}` | Library folder for `upload` (placeholders: `number`, `repository`, `owner`, `repo`, `provider`). |
| `max_size_mb` | `25` | Larger files stay plain links. |
| `link_issue` | `true` | Add the issue itself as the first link. Needed to re-link tasks when the state file is lost. |

Uploaded files are detected for each provider: GitHub `user-attachments`
and `user-images`, Gitea/Forgejo `/attachments/<uuid>` and issue assets,
GitLab `/uploads/<secret>/<file>`. Files that cannot be downloaded, for
example private GitHub attachments that need a browser session, are kept as
links.

### `task`

| Key | Default | Description |
|-----|---------|-------------|
| `title_template` | `#{number} {title}` | Placeholders: `number`, `title`, `repository`, `owner`, `repo`, `provider`, `state`. In two-way mode the template is stripped again when a title comes back from Planner. |
| `description` | `true` | Write the issue body as plain text. |
| `footer` | `true` | Append `Synced from <provider> <repo>#<n>: <url>`. |
| `checklist` | `true` | Turn `- [ ]` task-list items (up to 20) into checklist items. They are then left out of the description. |
| `due_date` | `true` | Use the issue's due date (Gitea, Forgejo, GitLab) or its milestone's due date. Date-only values are set to 12:00 UTC so the day is the same in every time zone. |
| `preview` | `automatic` | Card preview: `automatic`, `noPreview`, `checklist`, `description`, `reference`. |

### `planner_to_git`

Used when `direction` is `planner-to-git` or `both`.

| Key | Default | Description |
|-----|---------|-------------|
| `fields` | `[state, title, assignees, labels, due_date, checklist]` | What may flow back to Git. `labels` covers categories, priority and the bucket label. Add `description` to replace the issue body with the Planner description. Markdown formatting is lost when you do this, so it is opt-in. |
| `create_issues` | `false` | Open an issue for each Planner task that is not linked to one yet. |
| `buckets` | `[]` | Only tasks in these buckets become issues (empty: all buckets). |
| `include_completed` | `false` | Also create issues for completed tasks. |
| `close_deleted` | `false` | Close the issue when its task is deleted in Planner. |

Tasks that already link to an issue of the repository, carry a git2planner footer, or belong to
another sync are never turned into issues. This is what prevents
duplicates when several repositories share one plan.
