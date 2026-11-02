"""Configuration file templates used by ``git2planner init``."""

from __future__ import annotations

_SOURCES = {
    "github": """\
    source:
      provider: github
      repository: owner/repo
      # url: https://github.example.com     # GitHub Enterprise Server only
      # token: read from $GITHUB_TOKEN when omitted
""",
    "gitea": """\
    source:
      provider: gitea
      url: https://gitea.example.com
      repository: owner/repo
      # token: read from $GITEA_TOKEN when omitted
""",
    "forgejo": """\
    source:
      provider: forgejo
      url: https://codeberg.org
      repository: owner/repo
      # token: read from $FORGEJO_TOKEN (or $GITEA_TOKEN) when omitted
""",
    "gitlab": """\
    source:
      provider: gitlab
      # url: https://gitlab.example.com     # self-managed GitLab only
      repository: group/subgroup/project
      # token: read from $GITLAB_TOKEN when omitted
""",
}

_TEMPLATE = """\
# git2planner configuration
# Documentation: https://github.com/francescodifilippo/git2planner/blob/main/docs/configuration.md
#
# ${{NAME}} is replaced by the environment variable NAME, ${{NAME:-value}} adds a default.

graph:
  # Microsoft Entra ID app registration (see docs/azure-setup.md).
  # tenant_id, client_id and client_secret default to $AZURE_TENANT_ID,
  # $AZURE_CLIENT_ID and $AZURE_CLIENT_SECRET.
  # tenant_id: 00000000-0000-0000-0000-000000000000
  # client_id: 00000000-0000-0000-0000-000000000000
  # auth: auto | client_secret | federated | github_oidc | device_code | access_token
  auth: auto

state:
  # Remembers which task belongs to which issue and what was synced last.
  path: .git2planner/state.json

# Settings shared by every sync (each sync may override any of them).
defaults:
  # git-to-planner: issues -> tasks | planner-to-git: tasks -> issues | both
  direction: git-to-planner
  # Which side wins when the same field changed in Git and in Planner: git | planner
  conflicts: git

  filters:
    state: open                 # issues that get a new task: open | closed | all
    labels: []                  # only issues carrying all of these labels
    exclude_labels: []
    include_pull_requests: false

  buckets:
    strategy: fixed             # fixed | label | milestone
    default: To do
    closed: Done                # closed issues move here (remove to keep their bucket)
    # mapping:                  # label -> bucket (label strategy), milestone -> bucket
    #   bug: Bugs

  categories:
    mode: auto                  # auto: each label gets a Planner label | explicit | off
    exclude: [priority/urgent, priority/high, priority/low]
    # mapping:                  # label -> Planner label name or slot (category1..category25)
    #   bug: Bug
    #   security: category2

  assignees:
    use_git_email: true         # match the public e-mail of the Git user
    # email_domain: contoso.com # fallback: <git login>@contoso.com
    # mapping:                  # Git login -> UPN, e-mail or Entra object id
    #   octocat: mona@contoso.com

  priority:
    mapping:
      priority/urgent: urgent
      priority/high: important
      priority/low: low
    default: medium

  attachments:
    mode: link                  # link | upload (to the group's SharePoint library) | off

  task:
    title_template: "#{{number}} {{title}}"
    checklist: true             # Markdown task lists become Planner checklists

  on_close: complete            # complete | delete | ignore

  planner_to_git:
    fields: [state, title, assignees, labels, due_date, checklist]
    create_issues: false        # open issues for tasks created in Planner

syncs:
  - name: {name}
{source}    plan_id: REPLACE-WITH-PLAN-ID   # run `git2planner plans --group <group id>`
"""


def example_config(provider: str = "github") -> str:
    """Return an annotated configuration file for ``provider``."""
    return _TEMPLATE.format(name="my-project", source=_SOURCES[provider])
