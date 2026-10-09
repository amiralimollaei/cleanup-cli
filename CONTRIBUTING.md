# Contributing

Bug reports, documentation improvements, and focused code changes are welcome.
See the [README](README.md) for the public overview and
[architecture guide](docs/architecture.md) for where responsibilities belong.

## Set up a checkout

Install [uv](https://docs.astral.sh/uv/) and the GTK native prerequisites in the
[installation guide](docs/installation.md). The project selects its development
Python version through [`.python-version`](.python-version).

```console
git clone https://github.com/amiralimollaei/cleanup-cli.git
cd cleanup-cli
uv sync --locked --group gui --group scipy
```

This installs the project into `.venv` with the default `dev` group, the GTK
Python bindings, and SciPy. The complete suite imports GTK and uses SciPy to
compare the built-in NumPy DCT fallback, even though both groups are optional
for end users. `--locked` keeps dependency installation aligned with `uv.lock`.

## Run from source

After synchronizing the environment, launch the installed source entry points:

```console
uv run --no-sync cleanup-cli --help
uv run --no-sync cleanup-cli deduplicate /path/to/photos
uv run --no-sync cleanup-cli webp /path/to/photos
uv run --no-sync cleanup-gui
```

The CLI examples are dry runs. Use a disposable image directory when developing
deletion or replacement behavior. `--no-sync` reuses the environment prepared
above; rerun the locked sync command after changing dependencies.

## Checks

Run the complete suite and check source types from the repository root:

```console
uv run --no-sync pytest -q
uv run --no-sync pyright src
git diff --check
```

GTK widget tests need a working display. On a desktop, run them in a session
where GTK can open a window. Without a display, those tests are skipped, so a
passing run with skips does not establish GUI coverage.

For a headless Linux run, install GTK and its Python bindings, Adwaita icons,
an SVG loader, fonts, Xvfb, `xvfb-run`, `xauth`, and `dbus-run-session`.
The release workflow installs these dependencies on Ubuntu 24.04 and uses a
virtual display with the Adwaita theme and normal scaling:

```console
GTK_THEME=Adwaita GDK_BACKEND=x11 GDK_SCALE=1 GDK_DPI_SCALE=1 \
  GSK_RENDERER=cairo GSETTINGS_BACKEND=memory \
  dbus-run-session -- xvfb-run -a -s "-screen 0 1920x1080x24" \
  uv run --no-sync pytest -q
```

Xvfb supplies the display without a desktop session. The virtual screen size
allows the window layout tests to resize their windows.
The release workflow also checks that GTK can open the display before running
pytest. For changes to appearance or interaction, launch the GUI and inspect
the affected flow; automated widget checks do not replace that inspection.

## Propose a change

Keep changes focused on the problem being addressed. Reuse the shared argument,
result, and form helpers when extending an existing tool, and place domain
behavior in models so both interfaces can use it. Preserve dry-run defaults and
describe any intentional changes to deletion, replacement, or result ordering.

In a pull request, explain the problem, the resulting behavior, and the checks
you ran. Mention GUI or platform behavior that remains unverified. Add a
regression test when it protects behavior a change can break: image matching,
file retention, dry runs, safe deletion and replacement, resource limits,
result delivery, or GUI responsiveness and shutdown. Prefer realistic scenarios
over separate checks for trivial helpers, static metadata, icon availability,
or exact styling values. Existing tests are often sufficient for a refactor
that preserves behavior; inspect icons, spacing, and themes in the running GUI.

For a bug report, include the package version or commit, operating system,
Python version, command or GUI settings, expected and observed behavior, and
the relevant output or traceback. A small reproducible image sample or a
screenshot can help with image and GUI problems. Remove personal information
from paths and images before sharing them.

Maintainers publishing packages can use the [release guide](docs/releasing.md).
