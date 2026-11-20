// Sets the colour scheme before the first paint (no white flash in dark mode). The saved choice
// comes as ?theme=system|light|dark in the URL; "system" follows the Windows app mode.
(function () {
  var m = /[?&]theme=(light|dark|system)\b/.exec(location.search);
  var mode = m ? m[1] : "system";
  var dark = mode === "dark" || (mode === "system" && window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.dataset.mode = mode;
})();
