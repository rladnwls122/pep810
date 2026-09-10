"""Parsing the tree that ``python -X importtime`` writes to stderr."""

from pep810.importtime import parse_importtime


SAMPLE = """\
import time: self [us] | cumulative | imported package
import time:       100 |        100 |     encodings.aliases
import time:       200 |        300 |   encodings
import time:        50 |        350 | codecs
"""


def test_importtime_parses_depth_from_indentation():
    tree = parse_importtime(SAMPLE)
    depths = {record.module: record.depth for record in tree.records}
    assert depths == {"encodings.aliases": 2, "encodings": 1, "codecs": 0}


def test_importtime_totals_only_roots():
    assert parse_importtime(SAMPLE).total_us == 350


def test_importtime_cost_falls_back_to_an_ancestor():
    tree = parse_importtime(SAMPLE)
    assert tree.cost_of("encodings.aliases.missing") == 100
    assert tree.cost_of("nothing") == 0


def test_importtime_keeps_unrelated_stderr():
    tree = parse_importtime(SAMPLE + "Traceback (most recent call last):\n")
    assert "Traceback" in tree.stderr and len(tree.records) == 3


def test_importtime_handles_empty_output():
    assert not parse_importtime("")
