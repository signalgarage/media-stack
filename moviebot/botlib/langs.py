"""Languages and the per-language movie libraries (MOVIE_LIBRARIES).

A title's original language decides which movie library it is saved in. Radarr reports the
language by name ({"id": 2, "name": "French"}), Jellyseerr and TMDB by ISO 639-1 code ("fr"),
so everything is normalised to the two-letter code first.

Pure functions only: the caller passes the MOVIE_LIBRARIES string and the roots in.
"""
from __future__ import annotations

# ISO 639-1 code -> the language name as Radarr and Sonarr spell it.
LANGUAGES = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "pl": "Polish", "cs": "Czech", "hu": "Hungarian", "ro": "Romanian",
    "el": "Greek", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic",
    "he": "Hebrew", "fa": "Persian", "hi": "Hindi", "pa": "Punjabi", "bn": "Bengali",
    "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "mr": "Marathi", "ur": "Urdu",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay", "tl": "Tagalog",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "bg": "Bulgarian", "hr": "Croatian",
    "sr": "Serbian", "sk": "Slovak", "sl": "Slovenian", "lt": "Lithuanian", "lv": "Latvian",
    "et": "Estonian", "is": "Icelandic", "ca": "Catalan",
}

# Older or regional codes that TMDB and the apps still use for the same languages.
_CODE_ALIASES = {"nb": "no", "nn": "no", "iw": "he", "in": "id", "cn": "zh", "fil": "tl"}

_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGES.items()}
_NAME_ALIASES = {"farsi": "fa", "filipino": "tl", "mandarin": "zh", "cantonese": "zh",
                 "norwegian bokmal": "no", "flemish": "nl"}


def lang_code(value) -> str:
    """ISO 639-1 code for a language, or '' when it is unknown.

    Accepts a code ('fr', 'FR', 'pt-BR'), a name ('French'), a name with a region
    ('Portuguese (Brazil)') or Radarr's {'id': .., 'name': ..} object.
    """
    if isinstance(value, dict):
        value = value.get("name") or value.get("code") or ""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if "(" in s:                                   # 'portuguese (brazil)' -> 'portuguese'
        s = s.split("(", 1)[0].strip()
    base = s.replace("_", "-").split("-", 1)[0]    # 'pt-br' -> 'pt'
    if base in LANGUAGES:
        return base
    if base in _CODE_ALIASES:
        return _CODE_ALIASES[base]
    return _NAME_TO_CODE.get(s) or _NAME_ALIASES.get(s, "")


def lang_name(code) -> str:
    """'fr' -> 'French'; unknown codes come back unchanged."""
    c = lang_code(code)
    return LANGUAGES.get(c, str(code or ""))


def _safe_folder(folder) -> str:
    """A folder below the media root: no absolute paths, no '..', no empty parts."""
    parts = [p for p in str(folder or "").strip().replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def parse_libraries(spec):
    """'fr=movies-fr, es=movies-es' -> ({'fr': 'movies-fr', 'es': 'movies-es'}, rejected).

    The language may be a code or a name. `rejected` lists the entries that were skipped
    (unknown language, unsafe or missing folder) so the caller can log them.
    """
    libs, rejected = {}, []
    for part in str(spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            rejected.append(part)
            continue
        lang, folder = part.split("=", 1)
        code, folder = lang_code(lang), _safe_folder(folder)
        if not code or not folder:
            rejected.append(part)
            continue
        libs[code] = folder
    return libs, rejected


def library_root(language, roots_by_code, default_root) -> str:
    """Library root for a title's original language: its MOVIE_LIBRARIES root, else the default."""
    code = lang_code(language)
    return (roots_by_code or {}).get(code) or default_root
