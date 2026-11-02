# Security policy

## Supported versions

Security fixes are released for the latest minor version.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/francescodifilippo/git2planner/security/advisories/new)
instead. You will get an answer within a week.

## Handling of secrets

- Prefer environment variables (`${NAME}` in the configuration) to secrets
  written into `git2planner.yml`.
- Git tokens are only sent to the configured provider's hosts, never to
  third-party attachment hosts.
- The device-code token cache is created with mode 0600. The state file
  contains ids, ETags and hashes but no credentials.
