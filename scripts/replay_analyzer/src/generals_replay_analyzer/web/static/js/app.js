(() => {
  "use strict";

  const openButton = document.querySelector("[data-command-palette-open]");
  const closeButton = document.querySelector("[data-command-palette-close]");
  const palette = document.querySelector("#command-palette");
  const feedback = document.querySelector("#app-feedback");

  if (!(openButton instanceof HTMLButtonElement) || !(closeButton instanceof HTMLButtonElement) || !(palette instanceof HTMLDialogElement)) {
    return;
  }

  const announce = (message) => {
    if (feedback) {
      feedback.textContent = message;
    }
  };

  openButton.addEventListener("click", () => {
    palette.showModal();
    closeButton.focus();
    announce("Navigation dialog opened");
  });
  closeButton.addEventListener("click", () => palette.close());
  palette.addEventListener("close", () => {
    openButton.focus();
    announce("Navigation dialog closed");
  });
})();
