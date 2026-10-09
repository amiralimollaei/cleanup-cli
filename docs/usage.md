# Using Image Cleanup

Image Cleanup provides two recursive image tools through `cleanup-cli` and the
optional GTK application, `cleanup-gui`. Both start in preview mode. See
[installation](installation.md) for setup or return to the
[project overview](../README.md).

## Command-line basics

Run either command with a folder to scan, including its subfolders:

```console
cleanup-cli deduplicate /path/to/photos
cleanup-cli webp /path/to/photos
```

When working from a source checkout, prefix these commands with `uv run`, for
example `uv run cleanup-cli deduplicate /path/to/photos`.

Each command prints results as work completes, followed by counts and a storage
total. For all available options, use:

```console
cleanup-cli --help
cleanup-cli deduplicate --help
cleanup-cli webp --help
```

## Finding duplicate images

Start with a preview:

```console
cleanup-cli deduplicate /path/to/photos
```

Each result names the file it would delete, the copy it would keep, the matching
distance, and the bytes that would be reclaimed. The final storage total is
potential savings during a preview.

### Matching and the similarity threshold

Matching uses a 256-bit perceptual hash for image structure and an average RGB
color signature. The hash comes from the 16-by-16 low-frequency corner of a
discrete cosine transform over a normalized 64-by-64 image. The color check
helps distinguish images with similar structure but different colors.

`--threshold` accepts an integer from **0 to 256**, with **0** as the default.
The matching distance is the larger of:

- The number of differing bits in the two perceptual hashes.
- The largest difference between their average RGB channels, scaled to the
  same 0–256 range.

Threshold 0 requires equal structural and color signatures. Equal signatures
do not establish that two files contain identical bytes or pixels. Increasing
the threshold allows more visual differences and may also match distinct
images. Choose a threshold by reviewing previews of your own images; there is
no threshold that guarantees every match is a duplicate.

For example, to inspect matches at threshold 16:

```console
cleanup-cli deduplicate /path/to/photos --threshold 16
```

For animated or other multi-frame images, deduplication analyzes the **first
frame**. It does not compare the complete animation or every embedded image.

### Which copy is kept

When images match, the tool ranks the copies by these criteria, in order:

1. More pixels, calculated as width × height.
2. A larger encoded file when pixel counts are equal.
3. The last path in [natural sort order](path-sorting.md) when both are equal.

File size is a tie-breaker, rather than a measurement of visual quality. Each
reported deletion matches a copy selected for retention.

### Deleting matches

After reviewing a preview, repeat the command with the same threshold and add
`--delete`:

```console
cleanup-cli deduplicate /path/to/photos --threshold 16 --delete
```

This performs a fresh scan and permanently deletes the selected matches.
There is no undo or CLI confirmation prompt. Keep backups before deleting
files. The tool checks each deletion candidate's identity against the file
analyzed during that run and refuses changed or missing candidates. An I/O
error can occur after earlier files have already been deleted; inspect the
output before repeating the command.

With `--delete`, the storage total reports bytes actually deleted.

### Signature cache

Successful image signatures are cached to speed up later scans:

- With `XDG_CACHE_HOME` set:
  `$XDG_CACHE_HOME/cleanup-cli/image-signatures/`.
- Otherwise: `~/.cache/cleanup-cli/image-signatures/`.

Each scanned directory has a JSON cache named from the SHA-256 hash of its
resolved path. Cached entries are reused when the file's device, inode, size,
and nanosecond modification time still match. New or changed files are
analyzed again. This check uses filesystem metadata rather than a fresh hash
of all file contents.

Cache data from an older signature algorithm is ignored. Missing, invalid, or
unreadable cache data causes the tool to analyze the images again. You can
delete the cache directory at any time; future scans rebuild it. Preview runs
also update the cache while leaving the scanned images in place.

## Checking and applying WebP conversions

Check a folder with the default quality of **80**:

```console
cleanup-cli webp /path/to/photos
```

The preview reads each eligible image, encodes a temporary WebP beside it,
checks the output dimensions and size, and removes the temporary file. It
keeps the original and retains no converted file. The folder must therefore
be writable, even for a preview.

A candidate that would produce a smaller file is reported as skipped with
`replacement not enabled (use --replace)`. Other skip messages explain cases
such as an existing destination or a WebP that would not be smaller. A preview
reports **0 converted** and **0 bytes saved**, because nothing was replaced.

### Quality and replacement

`--quality` accepts an integer from **0 to 100**. Higher values generally
preserve more detail and produce larger output files. The converter uses
lossy WebP encoding; choosing 100 does not select lossless encoding. Pixel
width and height are preserved.

To check a different quality:

```console
cleanup-cli webp /path/to/photos --quality 90
```

To apply replacements at that quality:

```console
cleanup-cli webp /path/to/photos --quality 90 --replace
```

For example, a successful replacement of `photo.jpg` creates `photo.webp` in
the same folder and deletes `photo.jpg`. A source is replaced only when the
output has the expected dimensions, uses fewer bytes, and the source identity
still matches the file that was inspected. Existing destination entries are
never overwritten, including directories and dangling symlinks.

Replacement has no undo or CLI confirmation prompt. Keep backups before
replacing originals. The reported savings count bytes removed by completed
replacements; a failure can occur after earlier replacements have succeeded.

Existing WebP images are ignored. Multi-frame images, including animations
and multi-picture JPEGs, are skipped to avoid discarding their extra frames
or embedded images.

## File selection

Both tools scan regular files in natural path order and exclude file symlinks.
They check recognized filename extensions before asking Pillow to decode a
file. Extension matching is case-insensitive.

Recognized extensions are:

```text
.jpg .jpeg .png .apng .gif .webp .bmp .tif .tiff
.pnm .ppm .pgm .pbm .pcx .dds .exr .hdr .fits .qoi
.jp2 .j2k .jls .jxl .xbm .xwd .sun .ras .ico .wbmp
```

Actual decoding support depends on the installed Pillow build. Files that
cannot be decoded are ignored. A file without a recognized extension is not
examined even if its contents are an image.

## Worker and memory controls

Both commands accept the same resource controls:

| Option | Meaning | Default |
| --- | --- | --- |
| `--max-workers N` | Maximum number of threads for hashing or conversion. | Automatic, based on CPU count, up to 32. |
| `--memory-limit-mb MiB` | Budget for estimated concurrent image processing memory. | Automatic, based on available memory. |

Both values must be positive integers. Despite the option's `-mb` spelling,
memory is measured in **MiB**: 1 MiB = 1,048,576 bytes.

For example, limit work to four threads and an estimated 512 MiB:

```console
cleanup-cli deduplicate /path/to/photos --max-workers 4 --memory-limit-mb 512
cleanup-cli webp /path/to/photos --max-workers 4 --memory-limit-mb 512
```

Before decoding, the tools read image dimensions and estimate the memory
needed for each image. Concurrent work starts only when its combined estimates
fit the budget, so large images may reduce concurrency below the worker
limit. The budget applies to estimated image work; total process memory also
includes other overhead.

The automatic budget uses a quarter of detected available memory, capped at
1,024 MiB. It takes Linux container memory limits into account when available
and falls back to 256 MiB if available memory cannot be determined.

An image whose individual estimate exceeds the budget is not decoded. WebP
reports this as a skip; deduplication leaves uncached over-budget images out
of the comparison. Valid cached signatures can still be reused. Increase the
budget if you need to process an image excluded for this reason.

## GTK application

Launch the installed application with:

```console
cleanup-gui
```

From a source checkout with GUI dependencies installed:

```console
uv run --group gui cleanup-gui
```

Choose **Duplicate Images** or **WebP Conversion** in the header, then enter
an image folder or use its folder chooser. Both tools include subfolders and
start in preview mode.

- **Duplicate Images:** choose a similarity threshold and click **Find
  Duplicates**. Results show which file would be deleted, which copy is kept,
  the distance, and potential savings. Enabling **Delete duplicates** changes
  the action to **Find & Delete** and asks for confirmation before running.
- **WebP Conversion:** choose a quality and click **Check Images**. Results
  explain why files were skipped, including candidates that need replacement
  enabled. Enabling **Replace originals** changes the action to **Convert &
  Replace** and asks for confirmation before running. Completed replacements
  show their destination and storage savings.

The **Workers** and **Memory** controls follow the resource rules above.
Leave **Auto** selected to use automatic limits, or clear it to enter an
explicit thread count or MiB value.

Progress and completed results appear while the tool runs, with a cumulative
storage summary. Settings and the action button are disabled during a run.
Hover over a truncated summary to read its full text.

Drag the divider to resize the settings and results panels; each scrolls
independently. Divider positions are shared between tools. Narrow, tall
windows stack the panels, while shorter windows keep them side by side.
Use **Reset Panel Sizes** in the main menu to restore the defaults.

The application uses the system's symbolic icons and follows GNOME's
light/dark preference, including changes while it is open.
