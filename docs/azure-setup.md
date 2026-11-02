# Microsoft Entra ID setup

git2planner talks to Microsoft Planner through Microsoft Graph. It needs an
app registration in your Microsoft Entra ID (Azure AD) tenant. Choose one of
the two models below.

| Model | Best for | Acts as |
|-------|----------|---------|
| **Application permissions** (`client_secret`, `federated`, `github_oidc`) | CI, servers, scheduled jobs | the app itself (tasks show "created by git2planner") |
| **Delegated permissions** (`device_code`) | trying it out, personal use | you |

## 1. Register the application

1. Open the [Entra admin center](https://entra.microsoft.com) and go to
   **Identity → Applications → App registrations → New registration**.
2. Name it `git2planner`, keep *Accounts in this organizational directory only*,
   and select **Register**.
3. Copy the **Application (client) ID** and the **Directory (tenant) ID**.

## 2a. Application permissions

1. Go to **API permissions → Add a permission → Microsoft Graph → Application permissions**
   and add:
   - `Tasks.ReadWrite.All`
   - `GroupMember.Read.All`
   - `User.Read.All`
   - `Sites.ReadWrite.All` (only for `attachments.mode: upload`)
2. Select **Grant admin consent**.
3. Create a credential, using one of these:
   - **Client secret**: under **Certificates & secrets → New client secret**,
     copy the value into `AZURE_CLIENT_SECRET`.
   - **GitHub Actions without secrets**: under **Certificates & secrets →
     Federated credentials → Add credential → GitHub Actions deploying Azure
     resources**, enter your organisation, repository and entity (for example
     *Branch* `main`). Then set `graph.auth: github_oidc` and give the
     workflow `permissions: id-token: write`.
   - **Kubernetes workload identity**: add a federated credential for the
     service account and set `graph.auth: federated`. The token file is
     taken from `AZURE_FEDERATED_TOKEN_FILE`.

```bash
export AZURE_TENANT_ID=<tenant id>
export AZURE_CLIENT_ID=<client id>
export AZURE_CLIENT_SECRET=<secret>
```

## 2b. Delegated permissions (device code)

1. Under **Authentication**, set **Allow public client flows** to *Yes*.
2. Under **API permissions → Microsoft Graph → Delegated permissions**, add
   `Tasks.ReadWrite`, `GroupMember.Read.All`, `User.ReadBasic.All`,
   `Files.ReadWrite.All` and `offline_access`, then grant consent if your
   tenant requires it.
3. Sign in once:

```bash
export AZURE_CLIENT_ID=<client id>
git2planner login
```

The refresh token is cached in `~/.cache/git2planner/token.json` with mode
0600. Run `git2planner logout` to remove it.

## 3. Find the plan

Planner plans belong to a Microsoft 365 group. Its id is shown in the Entra
admin center under **Groups**.

```bash
git2planner plans --group <group id>     # application permissions
git2planner plans                        # delegated: plans you can see
git2planner describe-plan <plan id>      # buckets and labels
```

You can also open the plan in the browser: the URL contains `.../plan/<plan id>`.

## 4. Check everything

```bash
git2planner validate
```

`validate` signs in, reads the repository, the plan, its buckets and labels,
and the group members. When uploads are enabled it also checks the group's
document library. Any missing permission is reported with the call that
failed.

## Notes

- Assignees must be members of the plan's group, otherwise Planner rejects
  them. git2planner skips non-members with a warning.
- Guests are matched through their `mail` attribute.
- With application permissions, tasks and comments appear as created by the
  application.
