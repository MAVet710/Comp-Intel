# Comp-Intel – Dispensary Menu Scanner

A Streamlit app for competitive intelligence on cannabis dispensary menus.

## Features

- **Dutchie GraphQL crawler** – discovers all categories, paginates every page, captures live GraphQL API responses, filters to in-stock products only, and returns one row per purchasable variant (size/weight).
- **MED + REC support** – scan medical and adult-use menus separately; each row is tagged with a `Menu_Type` column (`med` / `rec`).
- **Playwright browser mode** – for Dutchie and other JS-heavy menus (Jane, Weedmaps, Dispense) the app launches a headless Chromium browser, bypasses 21+ age gates, and captures live network responses.
- **HTML / JSON-LD fallback** – static parsing for simpler sites.
- **OCR fallback** – screenshot + Tesseract OCR as a last resort.
- **Excel export** – download all scanned data with one click.

## Output schema

| Column | Description |
|--------|-------------|
| Dispensary | Label entered by the user |
| Menu_Type | `med` / `rec` / blank |
| Category | Product category (flower, edibles, etc.) |
| Product | Product name |
| Price | Price per variant |
| Brand | Brand name (when available) |
| THC | THC % (when available) |
| CBD | CBD % (when available) |
| Size | Variant size / weight (when available) |
| SKU | Product / variant ID (when available) |
| Source | Where the data came from |
| Source_URL | The URL that was scanned |
| Engine | Detected menu engine |

## Setup

```bash
pip install -r requirements.txt
playwright install chromium   # required for Dutchie / JS-heavy menus
streamlit run app.py
```

## Usage

1. Paste a menu URL into the **Menu URL** field (generic) **or** use the
   dedicated **MED URL** / **REC URL** fields to scan both menu types in one
   click.
2. Enter a **Dispensary label** (e.g. `Nature's Medicines Fall River`).
3. Enable **browser mode** (recommended for Dutchie menus — requires
   `playwright install chromium`).
4. Enable **Debug mode** to see captured GraphQL response counts, category
   list, per-page product counts, and parse notes.
5. Hit **Scan & Add to Table**.
6. Repeat for other dispensaries, then **Download Excel**.

## Streamlit Cloud Deployment

When deploying to [Streamlit Community Cloud](https://streamlit.io/cloud):

- The `postBuild` script at the repo root is automatically executed by Streamlit
  Cloud during the build phase.  It runs `python -m playwright install chromium`,
  so **Playwright browser binaries are installed automatically** — no manual step needed.
- `packages.txt` lists the system-level apt packages required by Chromium on the
  Debian-based Streamlit Cloud images.  These are also installed automatically
  during the build.

### Troubleshooting: "Executable doesn't exist" at runtime

If the app shows a warning that the Playwright Chromium binary is missing:

1. **Check the build logs** – In the Streamlit Cloud dashboard open *Manage app →
   Logs* (or the build log tab) and search for `postBuild`.  You should see lines
   like:
   ```
   === postBuild: installing Playwright Chromium binary ===
   Downloading Chromium ...
   === postBuild: done ===
   ```
   If these lines are absent the script did not run.

2. **Force a clean rebuild** – Go to *Manage app → Reboot app* (or delete and
   re-deploy the app).  Streamlit Cloud caches the build environment; a clean
   rebuild re-runs `postBuild`.

3. **Verify the Playwright pin** – `requirements.txt` pins `playwright` to a
   specific version.  The browser binary installed by `postBuild` must match this
   version.  If you upgrade the pin, redeploy so `postBuild` installs the matching
   binary.

4. **Enable automatic runtime install (advanced)** – Add the secret
   `AUTO_INSTALL_PLAYWRIGHT = true` in *Manage app → Secrets*.  When set, the
   app will attempt a one-time `playwright install chromium` the first time a
   missing binary is detected, then retry the browser operation.  This is a
   safety net and should not replace a correct build setup.

## Notes

- Browser mode launches a real headless Chromium instance and makes live
  network requests to the target site. Use responsibly and in accordance with
  the target site's terms of service.
- The Dutchie crawler auto-detects `med` / `rec` from the URL path if you
  use the generic URL field.
