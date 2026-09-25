// Same-origin blocking script: applies the saved theme before paint under the production CSP.
(function () {
  var theme = 'dark';
  try {
    var saved = window.localStorage.getItem('kinship-theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch (error) {}
  var root = document.documentElement;
  root.classList.remove('dark', 'light');
  root.classList.add(theme);
  root.style.colorScheme = theme;
  root.setAttribute('data-theme', theme);
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#F4F6F9' : '#0B0F14');
})();