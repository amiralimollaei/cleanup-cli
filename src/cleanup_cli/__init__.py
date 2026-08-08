import argparse
from pathlib import Path

from .abstractions import (
    DirectoryIndexer,
    DirectoryScanner,
    DistanceMetric,
    FileAnalyzer,
    IndexedFile,
    NaturalPathOrderer,
    PathOrderer,
    RecursiveDirectoryIndexer,
    RecursiveDirectoryScanner,
)
from .image_duplicates import (
    DeduplicationOptions,
    DirectoryDeduplicator,
    Duplicate,
    DuplicateDetector,
    FileRemover,
    ImageSignature,
    ImageSignatureDistance,
    LocalFileRemover,
    PyAVImageSignatureAnalyzer,
    ReverseDuplicateDetector,
    deduplicate_directory,
    find_duplicates,
    hamming_distance,
    image_signature,
    index_images,
    perceptual_hash,
)
from .image_webp import (
    DecodedImage,
    PyAVWebPCodec,
    WebPCodec,
    WebPConversion,
    WebPDirectoryConverter,
    WebPOptions,
    WebPSkip,
    convert_directory_to_webp,
)
from .path_sort import path_number_key, sort_numbered_paths


__all__ = [
    "Duplicate",
    "DuplicateDetector",
    "DeduplicationOptions",
    "DirectoryDeduplicator",
    "DirectoryIndexer",
    "DirectoryScanner",
    "DistanceMetric",
    "DecodedImage",
    "FileAnalyzer",
    "FileRemover",
    "ImageSignature",
    "ImageSignatureDistance",
    "IndexedFile",
    "LocalFileRemover",
    "NaturalPathOrderer",
    "PathOrderer",
    "PyAVImageSignatureAnalyzer",
    "PyAVWebPCodec",
    "RecursiveDirectoryIndexer",
    "RecursiveDirectoryScanner",
    "ReverseDuplicateDetector",
    "WebPCodec",
    "WebPConversion",
    "WebPDirectoryConverter",
    "WebPOptions",
    "WebPSkip",
    "convert_directory_to_webp",
    "deduplicate_directory",
    "find_duplicates",
    "hamming_distance",
    "image_signature",
    "index_images",
    "main",
    "path_number_key",
    "perceptual_hash",
    "sort_numbered_paths",
]


def _add_duplicate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("directory", type=Path, help="directory to scan recursively")
    parser.add_argument(
        "--threshold",
        type=int,
        default=0,
        metavar="BITS",
        help="maximum structural/color distance from 0 to 64 (default: 0)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete duplicates; without this flag, only show a dry run",
    )


def _run_duplicates(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:

    try:
        duplicates = deduplicate_directory(
            args.directory,
            threshold=args.threshold,
            delete=args.delete,
        )
    except (NotADirectoryError, ValueError) as error:
        parser.error(str(error))

    action = "deleted" if args.delete else "would delete"
    for duplicate in duplicates:
        print(
            f"{action}: {duplicate.removed} "
            f"(keeping {duplicate.kept}, distance {duplicate.distance})"
        )
    print(f"{len(duplicates)} duplicate(s) {'deleted' if args.delete else 'found'}")


def _run_webp(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    try:
        conversions, skips = convert_directory_to_webp(
            args.directory, quality=args.quality
        )
    except (NotADirectoryError, ValueError) as error:
        parser.error(str(error))

    for conversion in conversions:
        saved = conversion.original_size - conversion.webp_size
        print(f"converted: {conversion.source} -> {conversion.destination} (saved {saved} bytes)")
    for skip in skips:
        print(f"skipped: {skip.path} ({skip.reason})")
    print(f"{len(conversions)} image(s) converted, {len(skips)} image(s) skipped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up and optimize image directories.")
    subparsers = parser.add_subparsers(dest="command")

    duplicate_parser = subparsers.add_parser(
        "duplicates", help="find or remove perceptually duplicate images"
    )
    _add_duplicate_arguments(duplicate_parser)

    webp_parser = subparsers.add_parser(
        "webp", help="recursively replace images with smaller WebP files"
    )
    webp_parser.add_argument("directory", type=Path, help="directory to convert recursively")
    webp_parser.add_argument(
        "--quality",
        type=int,
        default=80,
        metavar="0-100",
        help="WebP encoding quality (default: 80)",
    )

    # Preserve the original ``cleanup-cli DIRECTORY`` interface.
    import sys

    arguments = sys.argv[1:]
    if arguments and arguments[0] not in {"duplicates", "webp", "-h", "--help"}:
        legacy_parser = argparse.ArgumentParser(
            description="Recursively remove perceptually duplicate images."
        )
        _add_duplicate_arguments(legacy_parser)
        _run_duplicates(legacy_parser.parse_args(arguments), legacy_parser)
        return

    args = parser.parse_args(arguments)
    if args.command == "duplicates":
        _run_duplicates(args, duplicate_parser)
    elif args.command == "webp":
        _run_webp(args, webp_parser)
    else:
        parser.print_help()
