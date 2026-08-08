# CleanUp CLI

Command line tool for removing duplicate images, and compressing everything into webp for saving storage.

## Converting images to WebP

The `webp` subcommand recursively finds decodable images and replaces each one
with a `.webp` file in the same directory. Existing WebP images and non-image
files are ignored. Pixel dimensions are preserved, and the original file is
removed only after the WebP has been validated and is smaller. Existing
destination files are never overwritten.

```console
cleanup-cli webp /path/to/photos
```

The default WebP quality is 80. It can be changed from 0 through 100:

```console
cleanup-cli webp /path/to/photos --quality 90
```

## Removing duplicate images

`cleanup-cli` recursively scans a directory, decodes images with PyAV, and
computes a 64-bit perceptual hash (pHash) using a NumPy DCT. A normalized RGB
color signature is also checked because grayscale pHash alone cannot detect a
uniform color shift. Files that PyAV cannot decode as images are ignored.
Paths are naturally sorted using the rules below, and only the **last** sorted
path among matching images is kept.

The default is a dry run and does not change any files:

```console
cleanup-cli duplicates /path/to/photos
```

Use `--delete` to remove the duplicates reported by the dry run:

```console
cleanup-cli duplicates /path/to/photos --delete
```

The threshold is the maximum normalized structural or color distance. The
structural value is the Hamming distance between two 64-bit pHashes; average
RGB channel differences are mapped to the same 0 through 64 range. `0`
requires equal structural and color signatures; larger values tolerate more
visual change. A conservative starting point for resized or re-encoded copies
is 4:

```console
cleanup-cli duplicates /path/to/photos --threshold 4
```

The accepted range is 0 through 64. Perceptual hashes describe low-frequency
image structure, not byte equality. Higher thresholds increase both tolerance
and the chance of grouping distinct images, so review the dry-run output
before using `--delete`.

## Natural sorting of paths

`sort_numbered_paths` sorts a path by every number it contains, from left to
right, instead of sorting the path alphabetically. Every directory and
filename component is compared hierarchically:

```python
from cleanup_cli import sort_numbered_paths

paths = ["dir-10", "dir-2", "dir-1.5-xxx", "abc5-5-XYZ", "misc"]
ordered = sort_numbered_paths(paths)
# ["dir-1.5-xxx", "dir-2", "abc5-5-XYZ", "dir-10", "misc"]
```

`1.5` is treated as one exact decimal number, while names such as
`chapter-2-part-10` produce the numeric tuple `(2, 10)`. Numeric tuples are
compared lexicographically, so the first number has priority, then the
second, and so on. Names with no numbers are sorted alphabetically after
numbered names. Leading zeros compare numerically (`dir-02` and `dir-2`),
with spelling used only to break ties. For example, `1/dir-10`, `2/misc`,
and `100/dir-2` sort in that order because the first path component is
compared before the filename. Relative and absolute paths retain their path
components in the comparison.

Date/time names are validated and sorted chronologically. Supported forms are:

- year-first separated dates: `2026-08-08`, `2026_08_08`, `2026.08.08`
- day-first separated dates: `08-08-2026`, `08_08_2026`, `08.08.2026`
- compact dates: `20260808`
- optional separated times such as `17-30-00`, `17_30_00`, `17.30.00`, or
  `17:30:00` (seconds may be omitted)
- optional compact times, for example `20260808_173000`

Different representations of the same timestamp compare equally and use the
filename only as a deterministic tie-breaker. Invalid dates fall back to the
normal numeric rules instead of raising an error.
