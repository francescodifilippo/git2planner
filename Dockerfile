FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE CHANGELOG.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim
LABEL org.opencontainers.image.title="git2planner" \
      org.opencontainers.image.description="Sync Git issues with Microsoft Planner tasks" \
      org.opencontainers.image.source="https://github.com/francescodifilippo/git2planner" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl \
    && useradd --create-home --uid 10001 git2planner
USER git2planner
WORKDIR /work
ENTRYPOINT ["git2planner"]
CMD ["sync"]
