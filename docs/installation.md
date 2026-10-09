# Installation

Image Cleanup requires Python 3.12 or newer. The command-line package is
`cleanup-cli`; the optional Linux GTK application is installed with
`cleanup-gui`.

## Create a Python environment

Use a virtual environment to keep the application's Python dependencies
separate from the operating system's packages. The `python3` command below
must refer to Python 3.12 or newer.

```console
python3 -m venv ~/.venvs/image-cleanup
source ~/.venvs/image-cleanup/bin/activate
```

Activate this environment in each terminal where you want to use the commands.
On Linux distributions that package virtual environment support separately,
install that package before creating the environment.

## Command-line package

Install the CLI in the activated environment:

```console
python -m pip install cleanup-cli
cleanup-cli --help
```

See [usage](usage.md) for duplicate detection and WebP conversion examples.

## GTK application

The GUI needs GTK 4.10 or newer and PyGObject. Python package installation
builds the bindings against system libraries, so install the native
prerequisites before installing `cleanup-gui`.

The package lists below follow the
[PyGObject installation guide](https://pygobject.gnome.org/getting_started.html).
Use a distribution that satisfies the current
[PyGObject system requirements](https://pygobject.gnome.org/guide/sysdeps.html).
The Python development headers must match the interpreter used by your virtual
environment.

### Fedora

For an environment using Fedora's default Python:

```console
sudo dnf install gtk4 python3-devel gobject-introspection-devel \
  cairo-devel cairo-gobject-devel gcc pkgconf-pkg-config
```

If the environment uses another Python version, install its matching development
package instead of `python3-devel`. For example, the source checkout currently
uses Python 3.12, which needs `python3.12-devel`.

### Ubuntu and Debian

On releases that provide the required native library versions:

```console
sudo apt install libgirepository-2.0-dev gcc libcairo2-dev pkg-config \
  python3-dev gir1.2-gtk-4.0
```

Use the development package for your environment's Python version if it differs
from the system default. Other distributions can use the corresponding packages
listed in the PyGObject installation guide.

### Install and launch

With the virtual environment activated and native prerequisites installed:

```console
python -m pip install cleanup-gui
cleanup-gui
```

This installs the CLI as a dependency, so both commands are available in the
same environment. Launch the GUI from a graphical desktop session.

## Running from source

Source checkout setup, optional dependency groups, and development commands are
covered in [CONTRIBUTING.md](../CONTRIBUTING.md).

[Documentation index](README.md)
