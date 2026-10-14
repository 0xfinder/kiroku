// Run after `cd web && npm ci`. Only visits the supplied local static server.
// node site/tools/screenshots.mjs http://127.0.0.1:PORT /tmp/site-shots
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(
  new URL("../../web/package.json", import.meta.url),
);
const { chromium } = require("@playwright/test");
const [origin, output = "/tmp/site-shots"] = process.argv.slice(2);
assert(origin, "Supply the local static server URL");
const base = new URL(origin);
assert(
  ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname),
  "Use a loopback server",
);
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
try {
  for (const [name, width, height] of [
    ["phone", 390, 844],
    ["desktop", 1440, 900],
    ["small", 360, 800],
  ]) {
    const page = await browser.newPage({
      viewport: { width, height },
      reducedMotion: "reduce",
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("response", (response) => {
      if (!response.ok()) errors.push(`${response.status()} ${response.url()}`);
    });
    await page.route("**/*", async (route) => {
      if (new URL(route.request().url()).origin === base.origin)
        await route.continue();
      else {
        errors.push(`Third-party request: ${route.request().url()}`);
        await route.abort();
      }
    });
    for (const file of ["index", "404"]) {
      await page.goto(new URL(`/${file}.html`, base).href, {
        waitUntil: "networkidle",
      });
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
        `${file} overflows at ${width}px`,
      );
      assert.deepEqual(errors, [], `${file} at ${width}px`);
      await page.keyboard.press("Tab");
      assert.equal(
        await page
          .locator(".skip-link")
          .evaluate((link) => link === document.activeElement),
        true,
      );
      await page.keyboard.press("Escape");
      await page.locator("body").click({ position: { x: 1, y: 1 } });
      const destination = path.join(output, `${file}-${name}.png`);
      await page.screenshot({ path: destination, fullPage: true });
      console.log(destination);
    }
    await page.close();
  }
} finally {
  await browser.close();
}
