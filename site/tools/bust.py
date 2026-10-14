"""Update both HTML pages with the first 12 hex digits of each asset's SHA-256."""

import hashlib
import re
from pathlib import Path

site = Path(__file__).resolve().parents[1]
for asset in ("style.css", "motion.js"):
    version = hashlib.sha256((site / "assets" / asset).read_bytes()).hexdigest()[:12]
    for page in site.glob("*.html"):
        page.write_text(
            re.sub(
                rf"/assets/{re.escape(asset)}\?v=[a-zA-Z0-9]+",
                f"/assets/{asset}?v={version}",
                page.read_text(),
            )
        )
