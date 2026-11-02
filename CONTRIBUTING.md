# Contributing

Thanks for helping improve git2planner!

## Development setup

```bash
git clone https://github.com/francescodifilippo/git2planner
cd git2planner
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Checks

Run these before opening a pull request. CI runs the same commands on
Python 3.10–3.13, Linux, macOS and Windows.

```bash
ruff check .
ruff format --check .
mypy
pytest --cov
```

## Guidelines

- Keep changes focused and add tests for new behaviour. The engine is tested
  against the in-memory doubles in `tests/fakes.py`, and the HTTP clients
  with `responses`.
- New provider? Subclass `git2planner.providers.base.IssueProvider`, register
  it in `git2planner/providers/__init__.py` and add it to `PROVIDERS` in
  `config.py`.
- Document user-visible changes in `CHANGELOG.md` and, where relevant, in
  `docs/configuration.md`.

## Releasing

1. Update `__version__` in `src/git2planner/__init__.py` and the changelog.
2. Commit, then either push a tag (`git tag v1.2.3 && git push origin v1.2.3`)
   or start the **Release** workflow from the Actions tab, which tags the
   current commit with the version from `__init__.py`.
3. The release workflow builds the package, creates the GitHub release,
   moves the major tag (`v1`) used by the GitHub Action, and publishes the
   Docker image to GHCR. It also publishes to PyPI when the repository
   variable `PYPI_PUBLISH` is `true` and a trusted publisher is configured.
