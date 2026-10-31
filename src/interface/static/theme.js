"use strict";
// One theme setting for the main app, studios, and standalone pages.
(function () {
  const KEY = "aidon-theme";
  function normalize(value) { return value === "dark" ? "dark" : "light"; }
  function storedTheme() {
    try { return normalize(localStorage.getItem(KEY)); }
    catch (_) { return "light"; }
  }
  function apply(value) {
    const theme = normalize(value);
    document.documentElement.setAttribute("data-theme", theme);
    const setBody = () => {
      if (document.body) document.body.classList.toggle("dark", theme === "dark");
    };
    if (document.body) setBody();
    else document.addEventListener("DOMContentLoaded", setBody, { once: true });
  }
  function set(value) {
    const theme = normalize(value);
    try { localStorage.setItem(KEY, theme); } catch (_) { /* storage may be disabled */ }
    apply(theme);
    return theme;
  }
  function toggle() {
    const current = normalize(document.documentElement.getAttribute("data-theme") || storedTheme());
    return set(current === "dark" ? "light" : "dark");
  }
  apply(storedTheme());
  window.AidonTheme = { get: storedTheme, set, toggle };
  window.addEventListener("storage", function (event) {
    if (event.key === KEY || event.key === null) apply(storedTheme());
  });
})();
