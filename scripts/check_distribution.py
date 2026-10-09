"""Check installed artifacts and entry points before publishing."""

from contextlib import redirect_stdout
from importlib.metadata import distribution
from importlib.resources import files
from io import StringIO


def main() -> None:
    package = distribution("cleanup-cli")
    entry_points = {
        entry.name: entry
        for entry in package.entry_points
        if entry.group == "console_scripts"
    }
    cli = entry_points["cleanup-cli"].load()
    assert callable(cli)
    assert callable(entry_points["cleanup-gui"].load())
    assert files("cleanup_cli").joinpath("views/gui/style.css").is_file()

    for arguments in (["--help"], ["deduplicate", "--help"], ["webp", "--help"]):
        output = StringIO()
        with redirect_stdout(output):
            try:
                cli(arguments)
            except SystemExit as error:
                assert error.code == 0, error.code
        assert "usage:" in output.getvalue(), arguments

    print(f"cleanup-cli {package.version}: installed entry points and GUI stylesheet OK")


if __name__ == "__main__":
    main()
