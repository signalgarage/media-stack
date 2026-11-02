"""moviebot/botlib/langs.py: language codes and the per-language movie libraries."""
from __future__ import annotations

import pytest
from botlib import langs


@pytest.mark.parametrize("value, code", [
    ("fr", "fr"), ("FR", "fr"), ("French", "fr"), ("french", "fr"), ("pt-BR", "pt"), ("pt_br", "pt"),
    ("Portuguese (Brazil)", "pt"), ({"id": 2, "name": "French"}, "fr"), ({"code": "es"}, "es"),
    ("nb", "no"), ("iw", "he"), ("Farsi", "fa"), ("Mandarin", "zh"),
    ("Original", ""), ("Klingon", ""), ("", ""), (None, ""),
])
def test_lang_code(value, code):
    assert langs.lang_code(value) == code


def test_lang_name():
    assert langs.lang_name("fr") == "French"
    assert langs.lang_name("French") == "French"
    assert langs.lang_name("xx") == "xx"            # unknown codes come back unchanged


def test_language_table():
    assert len(langs.LANGUAGES) == 48
    assert all(len(code) == 2 and code.islower() for code in langs.LANGUAGES)


def test_parse_libraries_accepts_codes_and_names_and_rejects_unsafe_folders():
    libs, rejected = langs.parse_libraries("fr=movies-fr, Spanish=/movies-es/, de=../outside, zz=foo, it=, junk")
    assert libs == {"fr": "movies-fr", "es": "movies-es"}
    assert rejected == ["de=../outside", "zz=foo", "it=", "junk"]
    assert langs.parse_libraries("") == ({}, [])
    assert langs.parse_libraries(None) == ({}, [])
    assert langs.parse_libraries("fr=films/./french//")[0] == {"fr": "films/french"}


def test_library_root():
    roots = {"fr": "/data/movies-fr"}
    assert langs.library_root("fr", roots, "/data/movies") == "/data/movies-fr"
    assert langs.library_root({"name": "French"}, roots, "/data/movies") == "/data/movies-fr"
    assert langs.library_root("English", roots, "/data/movies") == "/data/movies"
    assert langs.library_root(None, roots, "/data/movies") == "/data/movies"
    assert langs.library_root("fr", None, "/data/movies") == "/data/movies"
