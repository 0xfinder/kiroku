(() => {
  const root = document.documentElement;
  if (!root.classList.contains("motion")) return;
  const media = matchMedia("(prefers-reduced-motion: no-preference)");
  const events = ["scroll", "resize", "hashchange", "pageshow"];
  let observer, queued;
  const stop = () => {
    cancelAnimationFrame(queued);
    observer?.disconnect();
    events.forEach((event) => window.removeEventListener(event, schedule));
    media.removeEventListener("change", disable);
  };
  const disable = () => { root.classList.remove("motion"); stop(); };
  const pending = new Set(document.querySelectorAll("[data-reveal]"));
  const reveal = (el) => {
    el.classList.add("revealed");
    pending.delete(el);
    observer.unobserve(el);
    if (!pending.size) stop();
  };
  const check = () => {
    // Catch reveals skipped by anchor jumps or missed observer callbacks.
    [...pending].filter((el) => el.closest(".hero, .error-main") || el.getBoundingClientRect().top <= innerHeight + 80).forEach(reveal);
  };
  const schedule = () => {
    cancelAnimationFrame(queued);
    if (pending.size) queued = requestAnimationFrame(check);
  };
  try {
    if (!media.matches) { disable(); return; }
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (entry.isIntersecting) reveal(entry.target); });
    }, { rootMargin: "80px" });
    pending.forEach((el) => {
      el.style.setProperty("--delay", `${el.dataset.reveal}ms`);
      observer.observe(el);
    });
    events.forEach((event) => window.addEventListener(event, schedule, { passive: true }));
    media.addEventListener("change", disable);
    if (!pending.size) stop();
    schedule();
    clearTimeout(window.motionTimeout);
  } catch {
    disable();
  }
})();
