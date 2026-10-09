# Natural sorting of paths

Image Cleanup uses natural path sorting to make numbered filenames and
folders follow numeric order. This ordering also determines which duplicate
is kept when pixel count and file size are equal. See
[usage](usage.md#which-copy-is-kept) for the complete retention policy or
return to the [project overview](../README.md).

## Using the library

`sort_numbered_paths` accepts an iterable of strings or path objects and
returns a new sorted list. Original values and their types are preserved.

```python
from cleanup_cli import sort_numbered_paths

paths = ["dir-10", "dir-2", "dir-1.5-xxx", "abc5-5-XYZ", "misc"]
ordered = sort_numbered_paths(paths)
# ["dir-1.5-xxx", "dir-2", "abc5-5-XYZ", "dir-10", "misc"]
```

Use `path_number_key` with Python's `sorted` or `list.sort` when you need the
key directly:

```python
from pathlib import Path

from cleanup_cli import path_number_key

paths = [Path("2/item-1"), Path("1/item-100"), Path("1/item-2")]
ordered = sorted(paths, key=path_number_key)
# [Path("1/item-2"), Path("1/item-100"), Path("2/item-1")]
```

## Numeric and alphabetical rules

Each directory component and the filename are compared from left to right.
For an ordinary component, every number is extracted in the order it appears:

| Name | Numeric values |
| --- | --- |
| `dir-1.5` | `(1.5,)` |
| `chapter-2-part-10` | `(2, 10)` |
| `abc5-5-XYZ` | `(5, 5)` |

Decimals use exact decimal comparisons. Numeric tuples are compared
lexicographically: the first number takes priority, then the second, and so
on. This puts `item-1-2` before `item-1-10`.

Components containing numbers come before components without numbers.
Numberless components sort alphabetically using case-insensitive comparison.
When the numeric values match, spelling breaks the tie, first ignoring case
and then using the original spelling. Leading zeros therefore do not change
numeric value, but provide a deterministic order:

```text
dir-002
dir-02
dir-2
```

Parent directories participate fully. For example, these paths sort in this
order even though their filenames have different numbers:

```text
1/dir-10
2/misc
100/dir-2
```

Relative and absolute paths retain their components in the comparison. The
helper does not resolve paths or access the filesystem.

## Dates and times

A component containing a recognized valid date uses its normalized timestamp
as the numeric key. Year, month, day, hour, minute, and second are compared
chronologically, so year-first and day-first names can share the same order.

| Form | Examples |
| --- | --- |
| Year-month-day | `2026-08-08`, `2026_08_08`, `2026.08.08` |
| Day-month-year | `08-08-2026`, `08_08_2026`, `08.08.2026` |
| Compact year-month-day | `20260808` |
| Separated date with time | `2026-08-08T17:30:00`, `2026_08_08_17_30_00`, `08.08.2026-17.30` |
| Compact date with compact time | `20260808_173000`, `20260808T173000`, `20260808173000` |

Separated times accept colon, hyphen, underscore, or dot separators; seconds
may be omitted. Compact times require hour, minute, and second. Dates without
a time use midnight, and separated times without seconds use zero seconds.

For example:

```text
photo-2025-12-31_23-59-59.jpg
photo-2026-01-01.jpg
photo-2026_08_08_09_15_00.jpg
photo-2026.08.08-17.30.00.jpg
```

Representations of the same timestamp have equal numeric keys, with component
spelling used to make their final order deterministic. Once a valid date is
recognized, that component uses the timestamp rather than its other numeric
labels. Invalid dates fall back to ordinary numeric sorting when no valid date
is found; they do not raise an error.
