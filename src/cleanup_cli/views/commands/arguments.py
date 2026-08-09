"""Argument types shared by CLI subcommands."""

import argparse


def positive_int(value: str) -> int:
    """Parse an integer that is valid as a worker count."""

    workers = int(value)
    if workers < 1:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return workers