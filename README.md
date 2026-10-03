# Computational Connectomics Lab Website

Source for the lab website, built with [Quarto](https://quarto.org) and published to
[www.compconnectome.org](https://www.compconnectome.org).

## Structure

| Path | Purpose |
|---|---|
| `_quarto.yml` | Site config: title, navbar links, theme, CSS |
| `index.qmd` | Research (home) page |
| `people.qmd` | Lab members and alumni |
| `publications.qmd` | Full publication list, rendered from `publications.bib` |
| `resources.qmd` | Public code/datasets/tools |
| `openings.qmd` | Recruiting status |
| `publications.bib` | BibTeX source of truth for publications |
| `chicago-author-date-reverse-chrono.csl` | Citation style (newest first) used on the Publications page |
| `scripts/update_bib.py` | Adds new works from ORCID to `publications.bib` (see below) |
| `scripts/bib-exclude.txt` | Works the update script must never add (e.g. the PhD thesis) |
| `scripts/stack_videos.py` | Trims, crops and stacks videos into one web video (see below) |
| `pyproject.toml` | Ruff/mypy settings for `scripts/` only |
| `styles.css` | Site-wide custom CSS |
| `images/` | Images referenced from `.qmd` pages |
| `CNAME` | Custom domain for GitHub Pages |
| `_site/` | Rendered output (generated, git-ignored — never edit by hand) |

## Editing content

Each nav page is a `.qmd` file — plain Markdown with optional Pandoc divs/HTML. Edit
directly; no build step is required to change text.

- **Navbar / site title / theme** — `_quarto.yml`. Adding a page to the nav requires
  both the `.qmd` file and an entry under `website.navbar.left`.
- **Publications** — add/edit entries in `publications.bib` (BibTeX); the Publications
  page renders every entry automatically via `nocite: '@*'`. Don't hand-write citations
  in `publications.qmd`. (`styles.css` also has a `.pub-highlight` class for
  hand-curated feature blurbs; none are in use yet, and they would not be pulled from
  the `.bib`.)
- **Pulling new publications** — `python scripts/update_bib.py` lists works on the
  ORCID record that are not yet in `publications.bib` (metadata from Crossref);
  add `--write` to append them. It only adds entries, never edits or deletes, and
  skips anything already present (same DOI, a linked preprint/published version, or
  the same title). Works that should never appear go in `scripts/bib-exclude.txt`
  (DOI or title per line). Review new entries before committing — check that
  preprints with a later published version weren't added twice. Only works on the
  ORCID record are found, so keep that record up to date.
- **Images** — put files under `images/` or `photos/` and reference
  with standard Markdown `![alt](images/...)`.
- **Stacked videos** — several views shown together (e.g. the fly simulation on
  the Research page) must be one video file; separate `<video>` elements drift
  out of sync. `scripts/stack_videos.py` builds one from the individual clips.

  One-time setup: the script needs an ffmpeg with VP9 support. Install the
  `imageio-ffmpeg` package, which bundles one and is found automatically, into
  the same Python that runs the script (`python -m pip` guarantees that; a bare
  `pip` may belong to a different environment):

      python -m pip install imageio-ffmpeg

  Alternatively pass `--ffmpeg PATH` or set `$FFMPEG` to a full ffmpeg build.
  conda-forge's Windows ffmpeg lacks VP9, so it can only write the `.mp4`.

  Put the individual clips in `local/`; they are only inputs, so they stay out
  of `images/` and the published site. Then run it from anywhere in the repo;
  options after an `-i FILE` apply to that input only:

      python scripts/stack_videos.py --direction h --size 400 \
          -i birdseye.mp4 \
          -i panoramic_eye.mp4 --start 2 \
          -i compound_eye.mp4 --start 2 --crop auto \
          -o sim_hstack

  Relative `-i` paths are read from `local/` and a relative `-o` is written to
  `images/` (here `images/sim_hstack.mp4` and `.webm`); absolute paths are used
  as given, and `--input-dir`/`--output-dir` change the folders.
  `--start`/`--end` trim each clip (shift `--start` to line clips up in time);
  `--crop auto` removes black borders, or give `--crop W:H:X:Y`. `--direction v`
  stacks top to bottom, `--size` is the panel height (h) or width (v), and
  `--gap N` adds spacing. Add `--preview` to write just a PNG of the layout
  (into `local/`) first. It writes `.mp4` and `.webm` and prints the `<source>` tags to paste
  into the page; list the two files under `project.resources` in `_quarto.yml`.
  The exact command for each video on the site is in the comment above its
  `<video>` tag.
- **Figure backdrop** — figures arrive in two forms, so pick a class per figure:

      ![caption](images/fig.png){.fig-plain}    <!-- no transformation (default) -->
      ![caption](images/fig.png){.fig-invert}   <!-- flip a white-paper figure dark -->
      ![caption](images/fig.png){.fig-light}    <!-- untouched, on a white card -->

  `.fig-plain` is the default and the only option that never alters colours — use
  it for figures already exported for a dark backdrop (transparent or dark
  background, light text), like `images/eyemap-dark.png`. Use `.fig-invert` for
  journal figures printed on white paper that you cannot re-export; hues survive
  but saturated colours come back lighter, so avoid it where an exact colour
  carries meaning, and use `.fig-light` there instead. To change the site-wide
  default, copy the three values out of the class you want into the `:root` block
  in `styles.css`.

- **Styling** — shared CSS lives in `styles.css`; Quarto theme is `darkly` (a dark
  Bootswatch theme, set in `_quarto.yml`). Bootswatch's dark themes recolour
  Bootstrap's text tokens but not its surface/border tokens, so `styles.css`
  resets those in a `:root` block — keep new CSS on those variables rather than
  hardcoding light-mode greys.

## Local preview

Requires the [Quarto CLI](https://quarto.org/docs/get-started/) installed locally.

```sh
quarto preview
```

Live-reloads in the browser as you edit `.qmd`/`.css` files. Use `quarto render` to do a
one-off full build into `_site/` without a running preview server.

## Deployment

Pushing to `main` triggers `.github/workflows/publish.yml`, which renders the site with
Quarto and publishes it to the `gh-pages` branch. GitHub Pages serves that branch at the
custom domain in `CNAME`. No manual deploy step — just push to `main`.
