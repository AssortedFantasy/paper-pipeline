document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector(".exit-form");
  if (!form) return;

  form.addEventListener("submit", (event) => {
    const activeJobs = Number.parseInt(form.dataset.activeJobs || "0", 10);
    if (
      activeJobs > 0 &&
      !window.confirm(
        `Paper Pipeline has ${activeJobs} active job${activeJobs === 1 ? "" : "s"}. ` +
        "Exit anyway? Remote Batch work will resume next time; local work will stop."
      )
    ) {
      event.preventDefault();
      return;
    }

    const button = form.querySelector("button[type='submit']");
    if (button) {
      button.disabled = true;
      button.textContent = "Exiting…";
    }
  });
});
