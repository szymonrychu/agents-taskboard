# agents-taskboard

Component `agents-taskboard` (python) of the workdir project. Its own tests, `MEMORY.md` and `ROADMAP.md` live here.

Run `mise run test`, `mise run lint` and `mise run fmt` from this directory.

## Commands

- `mise run test` / `lint` / `fmt`; `mise run test:chart` and `lint:chart` (helm render tests and `helm lint`).
- `taskboard migrate | serve-web | serve-service | notifier` (see README); the image entrypoint is `taskboard`.
- `uv run python -m agents_taskboard.api.openapi > openapi.json` after any route or schema change (a test fails on drift); agents-web syncs this file.
- Release: the `release` job of `.github/workflows/ci.yaml` runs last on a push to main and tags the image and the repo `vX.Y.Z` from conventional commits (`scripts/next_version.py`, byte-identical in every component that releases this way). Deploy only semver tags.
