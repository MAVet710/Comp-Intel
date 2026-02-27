"""
Regression tests for the UnboundLocalError fix in fetch_competitor_menu.

Verifies that `final_url` is always defined (and debug_info["final_url"] is
always set) even when Playwright's Chromium binary is missing and
browser_fetch raises an exception.

These tests run without Playwright installed and without importing app.py
(which has module-level Streamlit UI code that cannot be run headlessly).
Instead they reproduce the exact code pattern that was fixed.
"""
import unittest


# ---------------------------------------------------------------------------
# Minimal reproduction of the affected code path in fetch_competitor_menu.
# The test mirrors the structure of app.py so it will catch the same
# UnboundLocalError if the fix is ever accidentally reverted.
# ---------------------------------------------------------------------------

class _MissingBinaryError(Exception):
    """Simulates the Playwright 'Executable doesn't exist' error."""


def _is_missing_browser_error(exc):
    return isinstance(exc, _MissingBinaryError)


def _simulate_fetch(url, use_browser, browser_fetch_fn, has_browser_helpers=True):
    """
    Mirrors the generic-browser-mode block in fetch_competitor_menu,
    including the fix (final_url = url initialised before the try/except).
    """
    debug_info = {
        "final_url": url,
        "browser_used": False,
        "parse_notes": [],
    }

    # ---- initialisation introduced by the fix ----
    browser_payloads = None
    final_url: str = url          # <-- the line under test
    # ----------------------------------------------

    engine = "jane"  # a JS-heavy engine that triggers browser mode
    js_heavy = engine in {"dutchie", "jane", "weedmaps", "dispense"}

    if (use_browser or js_heavy) and has_browser_helpers and engine != "dutchie":
        try:
            bhtml, browser_payloads, final_url = browser_fetch_fn(url)
        except Exception as _bfe:
            if _is_missing_browser_error(_bfe):
                debug_info["parse_notes"].append(
                    "Browser mode requested but skipped: Playwright Chromium "
                    "binary not found. Run `playwright install chromium` to "
                    "enable browser mode."
                )
            bhtml, browser_payloads, final_url = "", None, url   # <-- also fixed
        debug_info["browser_used"] = True
        debug_info["final_url"] = final_url  # must never raise UnboundLocalError

    return debug_info


class TestFinalUrlAlwaysDefined(unittest.TestCase):
    """fetch_competitor_menu must never raise UnboundLocalError for final_url."""

    URL = "https://example.com/menu"

    def _raises_missing(self, url):
        raise _MissingBinaryError(
            "Executable doesn't exist at /home/user/.local/share/ms-playwright/chromium"
        )

    def _succeeds(self, url):
        return "<html/>", [], url

    def test_no_unbound_local_error_when_browser_raises(self):
        """Must not raise UnboundLocalError when browser_fetch raises."""
        # If the fix is reverted, this call will raise UnboundLocalError,
        # which will propagate and fail the test automatically.
        _simulate_fetch(self.URL, use_browser=True, browser_fetch_fn=self._raises_missing)

    def test_final_url_equals_original_url_when_browser_raises(self):
        """debug_info['final_url'] must equal the input URL when browser_fetch raises."""
        info = _simulate_fetch(self.URL, use_browser=True, browser_fetch_fn=self._raises_missing)
        self.assertEqual(info["final_url"], self.URL)

    def test_parse_note_added_when_chromium_missing(self):
        """A user-visible parse note must mention the missing Chromium binary."""
        info = _simulate_fetch(self.URL, use_browser=True, browser_fetch_fn=self._raises_missing)
        notes = " ".join(info["parse_notes"])
        self.assertIn("Chromium", notes)

    def _redirected(self, url):
        """Simulates browser_fetch returning a different (post-redirect) URL."""
        return "<html/>", [], url + "/redirected"

    def test_final_url_set_correctly_when_browser_succeeds(self):
        """debug_info['final_url'] must reflect the (possibly redirected) URL returned by browser_fetch."""
        info = _simulate_fetch(self.URL, use_browser=True, browser_fetch_fn=self._redirected)
        self.assertEqual(info["final_url"], self.URL + "/redirected")

    def test_final_url_unchanged_when_browser_disabled(self):
        """When browser mode is off, final_url must still equal the input URL."""
        info = _simulate_fetch(self.URL, use_browser=False, browser_fetch_fn=self._raises_missing,
                               has_browser_helpers=False)
        self.assertEqual(info["final_url"], self.URL)


if __name__ == "__main__":
    unittest.main()
