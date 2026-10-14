(() => {
  const root = document.documentElement;
  const media = matchMedia("(prefers-reduced-motion: no-preference)");
  if (!media.matches || !("IntersectionObserver" in window)) return;
  try {
    const pending = new Set();
    const add = (selector, step = 0, start = 0) => {
      document.querySelectorAll(selector).forEach((el, index) => {
        el.classList.add("reveal");
        el.style.setProperty("--delay", `${start + index * step}ms`);
        pending.add(el);
      });
    };
    add(".hero-copy > *, .error-main > *", 70);
    add(".notebook", 0, 220);
    add(".notebook-source, .notebook-entry", 130, 360);
    add(".section-heading, .why > div, .engineering > div, .section-caption, .work-in-progress");
    for (const group of [".features", ".measurements", ".stack", ".pipeline-path", ".pipeline-branches"])
      add(`${group} > *`, group === ".pipeline-path" ? 140 : 70);
    add(".metric", 70, 120);
    const reveal = (el) => {
      el.classList.add("revealed");
      pending.delete(el);
      observer.unobserve(el);
    };
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (entry.isIntersecting) reveal(entry.target); });
    }, { rootMargin: "80px" });
    const check = () => {
      // Catch skipped reveals.
      [...pending].filter((el) => el.closest(".hero, .error-main") || el.getBoundingClientRect().top <= innerHeight + 80).forEach(reveal);
    };
    let queued;
    const schedule = () => {
      cancelAnimationFrame(queued);
      if (pending.size) queued = requestAnimationFrame(check);
    };
    pending.forEach((el) => observer.observe(el));
    for (const event of ["scroll", "resize", "hashchange", "pageshow"])
      window.addEventListener(event, schedule, { passive: true });
    media.addEventListener("change", () => { root.classList.remove("motion"); observer.disconnect(); });
    root.classList.add("motion");
    root.getBoundingClientRect();
    schedule();
  } catch {
    root.classList.remove("motion");
  }
})();
