// Áp dụng theme trước khi trang hiển thị để không bị nháy màu.
(function () {
  try {
    var choice = localStorage.getItem("vp-theme") || "system";
    var dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  } catch (e) {
    document.documentElement.dataset.theme = "light";
  }
})();
