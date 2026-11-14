# Workspace page redesign and deployment note

New note; older docs are untouched.

## What changed in the page
- Tabs: Overview, Clean, Explore, Predict, Report. The Ask bar is sticky at the top; new answers are added above older ones.
- Start screen: drag-and-drop or choose a CSV, or load one of three sample datasets. The samples are small made-up tables, labelled as such. No real data is bundled.
- Theme toggle (light/dark), remembered per browser. Charts are drawn in both themes.
- Error messages show beside the control that caused them. Tables are collapsed by default; "Show all rows" expands with a sticky header and no nested scroll area.
- Charts: correlation heatmap, coloured category bars. Line charts for date trends come from the existing "Trend of ..." request.
- Results can be removed one at a time or cleared; cleaning steps stay.

## Known limits
- The heatmap is shown on the page only. PNG/PDF export lists the correlations as a table, and its bars are one colour.
- Not tested: Windows, multi-GB files, phones other than a narrow Chromium viewport.
- Local test runs used a stand-in test runner; GitHub CI is the authoritative result.

## Deployment caution
- The workspace needs a Python backend (pandas, optional scikit-learn). A Cloudflare Worker cannot run it. Options: a Python host (Render, Railway, Fly, a VPS), or a browser-only build with Pyodide (large download, a separate project).
- The line "Your file stays on this computer" is true only when the server runs on the user's own machine (127.0.0.1). If it is hosted, uploaded CSVs go to that server; the wording, privacy text, upload limits, authentication and data retention must change first. Nothing here has been deployed.
