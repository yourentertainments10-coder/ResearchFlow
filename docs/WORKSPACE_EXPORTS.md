# PNG and PDF exports

Two more exports for the local workspace, built **without a new dependency** (standard library + numpy).

| Export | Route | What it is |
|---|---|---|
| PNG chart | `GET /api/export/<id>.png` | One bar or line chart, 1280 px wide, white background |
| PDF report | `GET /api/export/report.pdf` | The same report as the HTML one, as a PDF |

## Rules (same as the other exports)
* Only analyses that passed their independent check are exported. An unverified analysis gets HTTP 409; a
  table-only analysis has no chart (404); the PDF lists failed analyses under "Not included" and shows none of their numbers.
* Every number drawn is read from the computed result table; both formats are laid out from one scene
  (`sie.workspace.scene`), which mirrors the SVG layout, so a picture cannot show a figure the table lacks.
* Deterministic: the same session gives the same bytes. No dates, no random ids, no metadata.
* Nothing leaves the computer; no network, no external fonts.

## How they are made
* `raster.py` draws rectangles, lines and dots into a numpy array and writes the PNG with `zlib` and `struct`.
  Text uses glyph bitmaps cut from DejaVu Sans (free licence, Bitstream Vera/DejaVu) by `tools/make_font.py`
  and stored compressed in `_font_data.py`. Pillow is needed only to regenerate that file, never to run the product.
* `pdf.py` writes the PDF structure by hand: objects, cross-reference table, zlib streams, the built-in Courier
  fonts (so line widths are exact and tables line up), charts as vector shapes.

## Limits
* Text outside ASCII is printed as `?` (accents are dropped first, so "Zürich" becomes "Zurich"). Non-Latin scripts
  are not shown.
* The PDF uses a monospaced font and plain layout; it is a record, not a designed document. Tables show the first
  25 rows and as many columns as fit; the CSV export has everything.
* Charts over the existing limits (25 bars, 400 line points) are replaced by a note, as in the SVG.
* No PDF/A, no accessibility tags, no bookmarks.
* The PNG is drawn without anti-aliased shapes (text and lines are pixel-exact, edges are hard).

## Tests
`test_workspace_png_pdf.py` decodes the PNG independently (CRC, zlib, filter bytes) and measures the bars against the
table values; it parses the PDF (cross-reference offsets, page count, content streams) and checks that every verified
result, hash and table number is in it and that unverified ones are not. Server tests cover the routes, 404/409 and
byte-identical repeat requests. Not tested: opening the files in third-party viewers beyond a manual check with
`pdftotext`/`pdftoppm` on a Titanic report.
