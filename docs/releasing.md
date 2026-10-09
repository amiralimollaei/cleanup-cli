# Releasing packages

[README](../README.md) · [Contributing](../CONTRIBUTING.md)

This guide is for maintainers publishing `cleanup-cli` and `cleanup-gui` to PyPI.
The normal release path is the existing
[`release.yml`](../.github/workflows/release.yml) GitHub Actions workflow.

## Prepare a release

Keep the version in the root [`pyproject.toml`](../pyproject.toml) aligned with
[`packages/cleanup-gui/pyproject.toml`](../packages/cleanup-gui/pyproject.toml).
Update the GUI package's `cleanup-cli` dependency when its minimum required
version changes. After updating package versions or dependencies, refresh the
root `uv.lock` with `uv lock` so the locked release setup uses the new metadata.

Review the changes and run the checks in [CONTRIBUTING](../CONTRIBUTING.md).
When the release commit is ready, create and push a tag matching both package
versions, for example:

```console
git tag v0.1.1
git push origin v0.1.1
```

Pushing a `v*` tag starts the workflow. The build job rejects the tag unless it
matches both versions exactly. Choose a new version for a new release; PyPI
does not permit replacing an already uploaded distribution.

## What the workflow checks

Before building distributions, the build job on Ubuntu 24.04 installs GTK native
prerequisites, Adwaita icons, SVG support, fonts, and locked development, GUI,
and SciPy dependencies. It creates a D-Bus session and Xvfb display, uses the
Adwaita theme at normal scaling, verifies that GTK can open the display, and
runs the complete pytest suite. This includes display-dependent GUI tests and
the NumPy/SciPy fallback comparison.

After the tests succeed, the job builds a wheel and source distribution for
each package, checks metadata with Twine, and checks the installed CLI wheel
and source distribution for their entry points and GUI stylesheet. It then
checks the publishing inputs with `uv publish --dry-run` and uploads the
distributions as one workflow artifact.

The publish job depends on the successful build job. It downloads those
artifacts and publishes each package using its own GitHub environment and
PyPI Trusted Publisher. A failed test or build prevents both publish jobs from
starting. The package publishes are independent, so if one succeeds and the
other fails, inspect the failed job and PyPI state before retrying.

## Configure Trusted Publishing

Create these GitHub environments and configure a separate PyPI Trusted
Publisher for each project:

| PyPI project | GitHub environment |
| --- | --- |
| `cleanup-cli` | `pypi-cleanup-cli` |
| `cleanup-gui` | `pypi-cleanup-gui` |

Both publishers use owner `amiralimollaei`, repository `cleanup-cli`, and
workflow filename `release.yml`. Each publisher's environment field must match
the table. The workflow requests `id-token: write` for the publish jobs and uses
`uv publish --trusted-publishing always`; normal releases do not need a stored
PyPI API token.

For new projects, add pending publishers from your PyPI account's
**Publishing** page. The distinct environments identify which project each
publisher should create. Pending publishers do not reserve project names.

## Optional first publication from a local checkout

If you need to claim the project names before running the tag workflow, the
first release can be built and uploaded locally. Complete the contribution
checks first and use empty output directories for the chosen release version:

```console
uv build --no-sources --out-dir dist/cleanup-cli
uv build --no-sources --project packages/cleanup-gui --out-dir dist/cleanup-gui
uvx --from twine==7.0.0 twine check --strict dist/cleanup-cli/* dist/cleanup-gui/*
read -rsp "PyPI API token: " UV_PUBLISH_TOKEN
export UV_PUBLISH_TOKEN
uv publish --trusted-publishing never dist/cleanup-cli/*
uv publish --trusted-publishing never dist/cleanup-gui/*
unset UV_PUBLISH_TOKEN
```

Creating projects this way requires a PyPI token scoped to the account because the
projects do not exist yet. After the successful first uploads, add the Trusted
Publishers above to the existing projects for future releases. Each name is
claimed only after a successful upload.
