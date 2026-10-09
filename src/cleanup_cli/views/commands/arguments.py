"""Argument parsing and resource options shared by CLI subcommands."""

import argparse


def positive_int(value: str) -> int:
    """Parse an integer greater than zero."""

    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def add_resource_arguments(
    parser: argparse.ArgumentParser, *, activity: str
) -> None:
    """Register the common worker and memory limits for an activity."""

    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=None,
        metavar="N",
        help=(
            f"maximum number of worker threads for {activity} "
            "(default: executor default)"
        ),
    )
    parser.add_argument(
        "--memory-limit-mb",
        type=positive_int,
        default=None,
        metavar="MiB",
        help=(
            f"maximum estimated memory for concurrent {activity} "
            "(default: auto)"
        ),
    )
