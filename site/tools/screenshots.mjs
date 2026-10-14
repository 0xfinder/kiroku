// Run after `cd web && npm ci`. Only visits the supplied local static server.
// node site/tools/screenshots.mjs http://127.0.0.1:PORT /tmp/site-shots
import assert from "node:assert/strict";
import { mkdir, rename } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(new URL("../../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const [origin, output = "/tmp/site-shots"] = process.argv.slice(2);
assert(origin, "Supply the local static server URL");
const base = new URL(origin);
assert(["127.0.0.1", "localhost", "[::1]"].includes(base.hostname), "Use a loopback server");
await mkdir(output, { recursive: true });
const browser = await chromium.launch();

async function allVisible(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, "Horizontal overflow");
  assert.deepEqual(await page.evaluate(() => {
    return [...document.querySelectorAll("h1, h2, h3, p, article, li, blockquote, figure, dl")]
      .filter((el) => {
        for (let parent = el; parent; parent = parent.parentElement) {
          const style = getComputedStyle(parent);
          if (style.opacity !== "1" || style.visibility === "hidden" || style.display === "none") return true;
        }
        return false;
      }).map((el) => el.textContent.trim().slice(0, 60));
  }), [], "Content must be fully visible");
}

async function scrollThrough(page) {
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  for (let y = 0; y < height; y += 550) {
    await page.evaluate((top) => window.scrollTo({ top, behavior: "instant" }), y);
    await page.waitForTimeout(350);
  }
  await page.waitForTimeout(1200);
  await allVisible(page);
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
}

try {
  for (const [name, width, height, options] of [
    ["desktop", 1440, 900, {}],
    ["phone", 390, 844, { isMobile: true, hasTouch: true }],
    ["reduced-motion", 390, 844, { reducedMotion: "reduce" }],
    ["no-js", 1440, 900, { javaScriptEnabled: false }],
  ]) {
    const animated = name === "desktop" || name === "phone";
    const context = await browser.newContext({
      viewport: { width, height }, reducedMotion: "no-preference", ...options,
      ...(animated ? { recordVideo: { dir: output, size: { width, height } } } : {}),
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("response", (response) => { if (!response.ok()) errors.push(`${response.status()} ${response.url()}`); });
    await context.route("**/*", async (route) => {
      if (new URL(route.request().url()).origin === base.origin) await route.continue();
      else { errors.push(`Third-party request: ${route.request().url()}`); await route.abort(); }
    });
    await page.goto(new URL("/index.html", base).href, { waitUntil: "load" });
    assert.equal(await page.locator("html").evaluate((el) => el.classList.contains("motion")), animated);
    if (animated) {
      assert.equal(await page.locator(".stack > div").first().evaluate((el) => getComputedStyle(el).opacity), "0");
      await page.waitForTimeout(150);
      const entrance = await page.locator(".hero h1").evaluate((el) => Number(getComputedStyle(el).opacity));
      assert(entrance > 0 && entrance < 1, "Hero must visibly animate on load");
      // Hero copy and all three example rows settle within the requested 1.4 seconds.
      await page.waitForTimeout(1200);
      assert.equal(await page.locator(".hero .reveal:not(.revealed)").count(), 0);
      assert.equal(await page.locator(".note-entry").evaluate((el) => getComputedStyle(el).opacity), "1");
      await scrollThrough(page);
      assert.deepEqual(await page.locator(".metric").allTextContents(), ["15 → 9", "18/18", "6/9"]);
    } else {
      await allVisible(page);
      assert.equal(await page.evaluate(() => document.getAnimations().length), 0);
    }
    const shot = path.join(output, `r2-${name}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    console.log(shot);
    const video = page.video();
    await context.close();
    if (video) {
      const destination = path.join(output, `r2-${name}.webm`);
      await rename(await video.path(), destination);
      console.log(destination);
    }
    assert.deepEqual(errors, [], name);
  }

  // Probe anchor jumps, unavailable/failed enhancement and preference changes.
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: "no-preference" });
  await page.goto(new URL("/index.html#measured", base).href);
  await page.waitForTimeout(1400);
  assert.equal(await page.locator(".features .reveal:not(.revealed)").count(), 0);
  await page.locator('nav a[href="#how-it-works"]').click();
  await page.waitForTimeout(1400);
  assert.equal(await page.locator(".pipeline-path .reveal:not(.revealed)").count(), 0);
  // Simulate a missed observer callback; the scroll fallback still reveals the page.
  await page.addInitScript(() => { window.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} }; });
  await page.reload();
  await scrollThrough(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await allVisible(page);
  assert.equal(await page.evaluate(() => document.getAnimations().length), 0);
  const unscopedHiddenRules = await page.evaluate(() => {
    const hidden = [];
    const inspect = (rules) => {
      for (const rule of rules) {
        if (rule instanceof CSSKeyframesRule) continue;
        if (rule instanceof CSSStyleRule && rule.style.opacity === "0" && rule.selectorText.split(",").some((selector) => !selector.includes(".motion"))) hidden.push(rule.selectorText);
        if (rule.cssRules) inspect(rule.cssRules);
      }
    };
    for (const sheet of document.styleSheets) inspect(sheet.cssRules);
    return hidden;
  });
  assert.deepEqual(unscopedHiddenRules, [], "Hidden content must be scoped to .motion");
  for (const replacement of ["missing", "throws"]) {
    const context = await browser.newContext();
    await context.addInitScript((kind) => {
      if (kind === "missing") delete window.IntersectionObserver;
      else window.IntersectionObserver = class { constructor() { throw new Error("Unavailable"); } };
    }, replacement);
    const fallback = await context.newPage();
    await fallback.goto(base.href);
    await allVisible(fallback);
    await context.close();
  }
  const failed = await browser.newPage();
  await failed.route("**/assets/motion.js?*", (route) => route.abort());
  await failed.goto(base.href);
  await allVisible(failed);
  await failed.close();

  for (const [name, width, height] of [["desktop", 1440, 900], ["phone", 390, 844]]) {
    const error = await browser.newPage({ viewport: { width, height }, reducedMotion: "no-preference" });
    await error.goto(new URL("/404.html", base).href);
    await error.waitForTimeout(1000);
    await allVisible(error);
    const shot = path.join(output, `r2-404-${name}.png`);
    await error.screenshot({ path: shot, fullPage: true });
    console.log(shot);
    await error.keyboard.press("Tab");
    assert.equal(await error.locator(".skip-link").evaluate((el) => el === document.activeElement), true);
    await error.close();
  }
  console.log("Motion, anchor jumps, fallback visibility, reduced motion and unchanged metrics passed.");
} finally {
  await browser.close();
}
