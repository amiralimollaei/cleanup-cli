from pathlib import Path

from cleanup_cli import path_number_key, sort_numbered_paths


def test_sorts_integer_names_numerically() -> None:
    paths = ["dir-100", "dir-2", "dir-10", "dir-1"]

    assert sort_numbered_paths(paths) == ["dir-1", "dir-2", "dir-10", "dir-100"]


def test_sorts_decimals_as_exact_numeric_values() -> None:
    paths = ["dir-2", "dir-1.50", "dir-1.5", "dir-1.05"]

    assert sort_numbered_paths(paths) == ["dir-1.05", "dir-1.5", "dir-1.50", "dir-2"]


def test_sorts_all_nested_components_lexicographically() -> None:
    paths = ["item-2-1", "item-1-10", "item-1-2", "item-1-1", "item-2"]

    assert sort_numbered_paths(paths) == [
        "item-1-1",
        "item-1-2",
        "item-1-10",
        "item-2",
        "item-2-1",
    ]


def test_handles_numbers_embedded_in_arbitrary_names() -> None:
    paths = ["abc5-5-XYZ", "chapter-2-part-10", "dir-2"]

    assert sort_numbered_paths(paths) == ["dir-2", "chapter-2-part-10", "abc5-5-XYZ"]


def test_numberless_names_are_alphabetical_and_after_numbered_names() -> None:
    paths = ["zebra", "Alpha", "dir-1", "beta"]

    assert sort_numbered_paths(paths) == ["dir-1", "Alpha", "beta", "zebra"]


def test_all_path_components_are_parsed_and_input_type_is_preserved() -> None:
    paths = [Path("100/dir-2"), Path("1/dir-10"), Path("2/misc")]

    assert sort_numbered_paths(paths) == [Path("1/dir-10"), Path("2/misc"), Path("100/dir-2")]


def test_parent_component_precedes_child_component() -> None:
    paths = ["2/item-1", "1/item-100", "1/item-2"]

    assert sort_numbered_paths(paths) == ["1/item-2", "1/item-100", "2/item-1"]


def test_leading_zeroes_are_numeric_ties_with_deterministic_spelling_order() -> None:
    paths = ["dir-2", "dir-002", "dir-02"]

    assert sort_numbered_paths(paths) == ["dir-002", "dir-02", "dir-2"]


def test_key_is_usable_directly_with_sorted() -> None:
    assert sorted(["x-10", "x-1"], key=path_number_key) == ["x-1", "x-10"]


def test_accepts_any_iterable() -> None:
    paths = (path for path in ["dir-10", "dir-2"])

    assert sort_numbered_paths(paths) == ["dir-2", "dir-10"]


def test_sorts_year_first_dates_and_times_chronologically() -> None:
    paths = [
        "photo-2026.08.08-17.30.00.jpg",
        "photo-2025-12-31_23-59-59.jpg",
        "photo-2026_08_08_09_15_00.jpg",
        "photo-2026-01-01.jpg",
    ]

    assert sort_numbered_paths(paths) == [
        "photo-2025-12-31_23-59-59.jpg",
        "photo-2026-01-01.jpg",
        "photo-2026_08_08_09_15_00.jpg",
        "photo-2026.08.08-17.30.00.jpg",
    ]


def test_sorts_day_first_dates_by_year_then_month_then_day() -> None:
    paths = ["31-01-2026.png", "01.12.2025.png", "02_01_2026.png"]

    assert sort_numbered_paths(paths) == [
        "01.12.2025.png",
        "02_01_2026.png",
        "31-01-2026.png",
    ]


def test_sorts_compact_date_time_names_chronologically() -> None:
    paths = ["IMG_20260808_173001.jpg", "IMG_20260101.jpg", "IMG_20260808_093000.jpg"]

    assert sort_numbered_paths(paths) == [
        "IMG_20260101.jpg",
        "IMG_20260808_093000.jpg",
        "IMG_20260808_173001.jpg",
    ]


def test_equivalent_date_formats_have_deterministic_tie_order() -> None:
    paths = ["2026_08_08.png", "08-08-2026.png", "20260808.png"]

    assert sort_numbered_paths(paths) == ["08-08-2026.png", "20260808.png", "2026_08_08.png"]


def test_invalid_date_like_name_falls_back_to_numeric_sorting() -> None:
    paths = ["2026-13-01.png", "2026-12-01.png"]

    assert sort_numbered_paths(paths) == ["2026-12-01.png", "2026-13-01.png"]