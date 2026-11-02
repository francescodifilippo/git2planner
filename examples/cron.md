# Running from cron or systemd

## cron

```cron
*/15 * * * * cd /opt/git2planner && . ./env && git2planner -q sync >> sync.log 2>&1
```

`env` holds the secrets:

```bash
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
export GITHUB_TOKEN=...
```

## systemd timer

```ini
# /etc/systemd/system/git2planner.service
[Unit]
Description=git2planner sync

[Service]
Type=oneshot
WorkingDirectory=/opt/git2planner
EnvironmentFile=/opt/git2planner/env
ExecStart=/usr/local/bin/git2planner -q sync
```

```ini
# /etc/systemd/system/git2planner.timer
[Timer]
OnCalendar=*:0/15
Persistent=true

[Install]
WantedBy=timers.target
```

The `env` file for systemd uses `NAME=value` lines without `export`.

## Long-running container

```bash
docker run -d --name git2planner --restart unless-stopped \
  --user "$(id -u):$(id -g)" -v /opt/git2planner:/work --env-file /opt/git2planner/env \
  ghcr.io/francescodifilippo/git2planner:1 sync --watch 900
```
