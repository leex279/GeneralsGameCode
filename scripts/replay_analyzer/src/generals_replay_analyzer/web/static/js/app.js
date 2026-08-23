(() => {
  "use strict";

  document.documentElement.classList.add("js-enhanced");

  const openButton = document.querySelector("[data-command-palette-open]");
  const closeButton = document.querySelector("[data-command-palette-close]");
  const palette = document.querySelector("#command-palette");
  const feedback = document.querySelector("#app-feedback");
  const shortcutLabel = "Ctrl+K";
  let lastInvoker = openButton;

  if (!(openButton instanceof HTMLButtonElement) || !(closeButton instanceof HTMLButtonElement) || !(palette instanceof HTMLDialogElement)) {
    return;
  }

  const announce = (message) => {
    if (feedback) {
      feedback.textContent = message;
    }
  };

  const openPalette = () => {
    if (!palette.open) {
      if (document.activeElement instanceof HTMLElement) {
        lastInvoker = document.activeElement;
      }
      palette.showModal();
      closeButton.focus();
      announce(`Navigation dialog opened with ${shortcutLabel}`);
    }
  };

  openButton.addEventListener("click", openPalette);
  closeButton.addEventListener("click", () => palette.close());
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const isEditing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && !isEditing) {
      event.preventDefault();
      openPalette();
    }
  });
  palette.addEventListener("close", () => {
    if (lastInvoker.isConnected) {
      lastInvoker.focus();
    } else {
      openButton.focus();
    }
    announce("Navigation dialog closed");
  });

  const filterButton = document.querySelector("[data-all-filters-open]");
  const filterCloseButton = document.querySelector("[data-all-filters-close]");
  const filterDialog = document.querySelector("#all-filters");
  let filterInvoker = filterButton;

  if (filterButton instanceof HTMLButtonElement && filterCloseButton instanceof HTMLButtonElement && filterDialog instanceof HTMLDialogElement) {
    filterButton.addEventListener("click", () => {
      filterInvoker = filterButton;
      filterDialog.showModal();
      filterCloseButton.focus();
      announce("All replay filters opened");
    });
    filterCloseButton.addEventListener("click", () => filterDialog.close());
    filterDialog.addEventListener("close", () => {
      filterInvoker.focus();
      announce("All replay filters closed");
    });
  }

  // TheSuperHackers @feature Leex 23/08/2026 Restore focus after closing an enhanced replay-import dialog. (#TBD)
  let importInvoker = null;
  document.addEventListener("click", (event) => {
    const target = event.target;
    const trigger = target instanceof Element ? target.closest("[data-import-dialog-open]") : null;
    if (trigger instanceof HTMLElement) {
      importInvoker = trigger;
    }
  });

  const activateImportDialog = (root) => {
    const dialog = root.querySelector("#import-dialog");
    const close = root.querySelector("[data-import-dialog-close]");
    if (!(dialog instanceof HTMLDialogElement) || !(close instanceof HTMLButtonElement)) {
      return;
    }
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      const host = document.querySelector("#import-modal-host");
      if (host instanceof HTMLElement) {
        host.replaceChildren();
      }
      if (importInvoker instanceof HTMLElement && importInvoker.isConnected) {
        importInvoker.focus();
      }
      announce("Replay import dialog closed");
    }, { once: true });
    dialog.showModal();
    close.focus();
    announce("Replay import dialog opened");
  };

  document.addEventListener("htmx:afterSwap", (event) => {
    const root = event.target instanceof Element ? event.target : document;
    activateImportDialog(root);
  });
})();
