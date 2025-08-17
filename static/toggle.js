const themeToggle = document.getElementById("theme-toggle");
const root = document.documentElement;

// Load saved theme
if (localStorage.getItem("theme")) {
  root.setAttribute("data-theme", localStorage.getItem("theme"));
  themeToggle.textContent = localStorage.getItem("theme") === "dark" ? "☀️" : "🌙";
}

themeToggle.addEventListener("click", () => {
  console.log('====================================');
  console.log('hello');
  console.log('====================================');
  let current = root.getAttribute("data-theme");
  let next = current === "dark" ? "light" : "dark";
  root.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  themeToggle.textContent = next === "dark" ? "☀️" : "🌙";
});
