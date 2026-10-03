"""Add new works from an ORCID record to publications.bib.

Pulls the list of works from the public ORCID API, fetches metadata for each DOI
from Crossref, and appends BibTeX entries for works not already in the .bib.
Existing entries are never modified or removed, so hand edits survive.

A work counts as already present, and is skipped, when any of these hold:
  - its DOI is in the .bib (eLife version suffixes such as ".1" are ignored);
  - Crossref links it to a DOI in the .bib (preprint of / same as / version of);
  - its title matches a .bib title, ignoring case, spaces and punctuation.
Works listed in scripts/bib-exclude.txt are always skipped.

Dry run by default; pass --write to modify the file. Standard library only:

    python scripts/update_bib.py            # show what would be added
    python scripts/update_bib.py --write    # add it
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ORCID_ID = "0000-0003-2869-4393"  # Arthur Zhao
REPO = Path(__file__).resolve().parent.parent
BIB_PATH = REPO / "publications.bib"
EXCLUDE_PATH = REPO / "scripts" / "bib-exclude.txt"
USER_AGENT = "coconeurolab-bib-updater/1.0 (https://www.coconeurolab.org)"

MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
KEY_STOPWORDS = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with"}
RELATION_TYPES = ("is-preprint-of", "is-same-as", "is-version-of", "has-version")

Json = dict[str, Any]


def fetch_json(url: str) -> Json:
    """Fetch a URL and parse the response body as JSON."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: Json = json.load(resp)
    return data


def norm_doi(doi: str) -> str:
    """Normalise a DOI for comparison: lowercase, no URL prefix, no eLife version."""
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return re.sub(r"^(10\.7554/elife\.\d+)\.\d+$", r"\1", doi)


def norm_title(title: str) -> str:
    """Reduce a title to lowercase ASCII letters and digits only, for comparison."""
    title = re.sub(r"<[^>]+>", "", html.unescape(title))
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", title.lower())


def read_bib(path: Path) -> tuple[str, set[str], set[str], set[str]]:
    """Read the .bib and return its text plus the DOIs, titles and keys it holds."""
    text = path.read_text(encoding="utf-8")
    dois = {norm_doi(d) for d in re.findall(r"^\s*doi\s*=\s*\{([^}]*)\}", text, re.M)}
    titles = {
        norm_title(t.replace("{", "").replace("}", ""))
        for t in re.findall(r"^\s*title\s*=\s*\{(.*)\},?\s*$", text, re.M)
    }
    keys = set(re.findall(r"^@\w+\{([^,\s]+),", text, re.M))
    return text, dois, titles, keys


def read_excludes(path: Path) -> tuple[set[str], set[str]]:
    """Read the exclusion list and return the DOIs and normalised titles in it."""
    dois: set[str] = set()
    titles: set[str] = set()
    if not path.exists():
        return dois, titles
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if re.match(r"^(https?://(dx\.)?doi\.org/)?10\.\d+/", line, re.I):
            dois.add(norm_doi(line))
        else:
            titles.add(norm_title(line))
    return dois, titles


def orcid_works(orcid: str) -> list[tuple[str, str | None]]:
    """List (title, DOI or None) for each distinct work on a public ORCID record."""
    data = fetch_json(f"https://pub.orcid.org/v3.0/{orcid}/works")
    works = []
    for group in data.get("group", []):
        summary = group["work-summary"][0]
        title = summary["title"]["title"]["value"]
        doi = next(
            (
                e["external-id-value"]
                for e in group["external-ids"]["external-id"]
                if e["external-id-type"] == "doi"
                and e.get("external-id-relationship") == "self"
            ),
            None,
        )
        works.append((title, doi))
    return works


def crossref(doi: str) -> Json:
    """Fetch a work's Crossref metadata record by DOI."""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/")
    message: Json = fetch_json(url)["message"]
    return message


def tex_escape(text: str) -> str:
    """Escape the characters that are special in BibTeX field values."""
    return re.sub(r"([&%#$_])", r"\\\1", text)


def tex_title(raw: str) -> str:
    """Convert a Crossref title to BibTeX, keeping italics and capitalised words."""
    text = html.unescape(re.sub(r"\s+", " ", raw)).strip()
    text = re.sub(r"<(i|em)>(.*?)</\1>", r"\\emph{\2}", text, flags=re.S)
    text = tex_escape(re.sub(r"<[^>]+>", "", text))
    # Brace every word after the first that has a capital letter, so a style
    # that sentence-cases titles cannot lowercase names like {Drosophila}.
    words = text.split(" ")
    for i, word in enumerate(words[1:], start=1):
        if re.search(r"[A-Z]", word) and not word.startswith("\\"):
            words[i] = re.sub(r"^(\W*)(.*?)(\W*)$", r"\1{\2}\3", word)
    return " ".join(words)


def tex_author(author: Json) -> str:
    """Format one Crossref author as "Family, Given" for BibTeX."""
    if "family" not in author:
        return "{" + tex_escape(author.get("name", "Anonymous")) + "}"
    family = tex_escape(author["family"])
    if " " in family:
        family = "{" + family + "}"
    given = author.get("given")
    if given:
        given = re.sub(r"\b([A-Z])(?=\s|-|$)", r"\1.", given)  # "M" -> "M."
    return f"{family}, {tex_escape(given)}" if given else family


def pub_date(meta: Json) -> list[int]:
    """Return [year, month?, day?] from the most print-like date Crossref has."""
    for field in ("published-print", "published-online", "issued", "posted"):
        parts = meta.get(field, {}).get("date-parts", [[None]])[0]
        if parts and parts[0]:
            return [int(p) for p in parts]
    return []


def ascii_word(text: str) -> str:
    """Fold a string to lowercase ASCII letters and digits."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def make_key(meta: Json, taken: set[str]) -> str:
    """Build a citation key like zhao2025eye, unique against the existing keys."""
    authors = meta.get("author") or [{}]
    first = authors[0].get("family") or authors[0].get("name") or "anon"
    date = pub_date(meta)
    year = str(date[0]) if date else "nd"
    title = re.sub(r"<[^>]+>", "", (meta.get("title") or [""])[0])
    word = next(
        (w for w in map(ascii_word, title.split()) if w and w not in KEY_STOPWORDS),
        "",
    )
    base = ascii_word(first) + year + word
    key, suffix = base, ord("b")
    while key in taken:
        key, suffix = base + chr(suffix), suffix + 1
    taken.add(key)
    return key


def bib_entry(meta: Json, key: str) -> str:
    """Render a Crossref record as a BibTeX entry in the file's house style."""
    kind = meta.get("type", "")
    container = (meta.get("container-title") or [""])[0]
    if kind == "posted-content":
        institution = (meta.get("institution") or [{}])[0].get("name")
        container = container or institution or meta.get("publisher", "")
    entry_type, container_field = {
        "journal-article": ("article", "journal"),
        "posted-content": ("article", "journal"),
        "proceedings-article": ("inproceedings", "booktitle"),
        "book-chapter": ("incollection", "booktitle"),
    }.get(kind, ("misc", "howpublished"))

    fields: list[tuple[str, str]] = [
        ("author", " and ".join(tex_author(a) for a in meta.get("author", []))),
        ("title", tex_title((meta.get("title") or [""])[0])),
        (container_field, tex_escape(html.unescape(container))),
        ("volume", meta.get("volume", "")),
        ("number", meta.get("issue", "")),
        ("pages", re.sub(r"(\d)-(\d)", r"\1--\2", meta.get("page", ""))),
    ]
    if not meta.get("page") and meta.get("article-number"):
        fields[-1] = ("pages", meta["article-number"])
    date = pub_date(meta)
    if date:
        fields.append(("year", str(date[0])))
    doi = meta["DOI"]
    fields += [("doi", doi), ("url", f"https://doi.org/{doi}")]
    if kind == "posted-content":
        fields.append(("note", "Preprint"))

    lines = [f"@{entry_type}{{{key},"]
    for name, value in fields:
        if value:
            lines.append(f"  {name:<7} = {{{value}}},")
        if name == "year" and len(date) > 1:
            lines.append(f"  {'month':<7} = {MONTHS[date[1] - 1]},")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    """Compare the ORCID record with the .bib and report or append new works."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="modify the .bib")
    parser.add_argument("--orcid", default=ORCID_ID, help="ORCID iD to read")
    parser.add_argument("--bib", type=Path, default=BIB_PATH, help="BibTeX file")
    parser.add_argument("-v", "--verbose", action="store_true", help="list skips")
    args = parser.parse_args()

    text, bib_dois, bib_titles, keys = read_bib(args.bib)
    ex_dois, ex_titles = read_excludes(EXCLUDE_PATH)
    try:
        works = orcid_works(args.orcid)
    except urllib.error.URLError as err:
        print(f"Could not read ORCID record {args.orcid}: {err}", file=sys.stderr)
        return 1

    new: list[str] = []
    skipped: list[str] = []
    for title, doi in works:
        label = f"{title[:70]} ({doi or 'no DOI'})"
        if norm_title(title) in ex_titles or (doi and norm_doi(doi) in ex_dois):
            skipped.append(f"excluded: {label}")
            continue
        if norm_title(title) in bib_titles:
            skipped.append(f"title already in .bib: {label}")
            continue
        if not doi:
            skipped.append(f"no DOI, add by hand if wanted: {label}")
            continue
        if norm_doi(doi) in bib_dois:
            skipped.append(f"DOI already in .bib: {label}")
            continue
        try:
            meta = crossref(doi)
        except urllib.error.URLError as err:
            skipped.append(f"Crossref lookup failed ({err}): {label}")
            continue
        related = {
            norm_doi(r["id"])
            for rel in RELATION_TYPES
            for r in meta.get("relation", {}).get(rel, [])
            if r.get("id-type") == "doi"
        }
        crossref_title = (meta.get("title") or [""])[0]
        if related & bib_dois or norm_title(crossref_title) in bib_titles:
            skipped.append(f"other version already in .bib: {label}")
            continue
        new.append(bib_entry(meta, make_key(meta, keys)))
        bib_dois.add(norm_doi(doi))
        bib_titles.add(norm_title(crossref_title))

    print(f"ORCID {args.orcid}: {len(works)} works, {len(new)} new.")
    for line in skipped:
        if args.verbose or not line.split(":", 1)[0].endswith("in .bib"):
            print(f"  skipped, {line}")
    if not new:
        return 0
    print("\n" + "\n\n".join(new) + "\n")
    if args.write:
        args.bib.write_text("\n\n".join(new) + "\n\n" + text, encoding="utf-8")
        print(f"Added {len(new)} entries to the top of {args.bib.name}.")
    else:
        print("Dry run; pass --write to add these entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
