export function mountRelayMemoryMode(root, options = {}) {
  if (!(root instanceof Element)) throw new TypeError("root must be an Element");

  const section = document.createElement("section");
  section.className = "relay-memory-mode";

  const heading = document.createElement("h2");
  heading.textContent = "Memory retrieval";

  const toggleLabel = document.createElement("label");
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.name = "memory_retrieval_enabled";
  toggle.checked = false;
  toggleLabel.append(toggle, " Enable memory retrieval mode");

  const disabledClaim = document.createElement("p");
  disabledClaim.textContent =
    "Without memory retrieval: save up to about 50% of premium-model compute.";
  const disabledPrice = document.createElement("p");
  disabledPrice.textContent =
    "Current Relay pricing is provided by your signed-in Relay account.";

  const enabledClaim = document.createElement("p");
  enabledClaim.textContent =
    "With memory retrieval: save up to about 90% of total model compute on long, continuous, history-rich tasks.";
  enabledClaim.hidden = true;
  const enabledPrice = document.createElement("p");
  enabledPrice.textContent =
    "Current Relay pricing is provided by your signed-in Relay account.";
  enabledPrice.hidden = true;

  const memoryValue = document.createElement("div");
  memoryValue.hidden = true;
  const memoryValueHeading = document.createElement("h3");
  memoryValueHeading.textContent = "Near-unlimited long-term memory";
  const memoryValueDescription = document.createElement("p");
  memoryValueDescription.textContent =
    "Your history can keep growing while Relay retrieves only what each task needs.";
  const memoryLimit = document.createElement("p");
  memoryLimit.textContent = "Subject to account storage and import limits.";
  memoryValue.append(memoryValueHeading, memoryValueDescription, memoryLimit);

  const included = document.createElement("p");
  included.textContent =
    "Initial memory preparation has no separate setup charge and is subject to your account import limits.";
  included.hidden = true;

  const preparation = document.createElement("div");
  preparation.className = "relay-memory-preparation";
  preparation.hidden = true;
  const progress = document.createElement("progress");
  progress.max = 100;
  progress.removeAttribute("value");
  progress.setAttribute("aria-label", "Memory preparation progress");
  const progressText = document.createElement("p");
  progressText.setAttribute("role", "status");
  progressText.setAttribute("aria-live", "polite");
  progressText.textContent = "Waiting to scan selected sources.";
  const timeNotice = document.createElement("p");
  timeNotice.textContent = "More memory takes longer to prepare.";
  preparation.append(progress, progressText, timeNotice);

  section.append(
    heading,
    toggleLabel,
    disabledClaim,
    disabledPrice,
    enabledClaim,
    enabledPrice,
    memoryValue,
    included,
    preparation,
  );
  root.append(section);

  function reflectMode() {
    disabledClaim.hidden = toggle.checked;
    disabledPrice.hidden = toggle.checked;
    enabledClaim.hidden = !toggle.checked;
    enabledPrice.hidden = !toggle.checked;
    memoryValue.hidden = !toggle.checked;
    included.hidden = !toggle.checked;
    preparation.hidden = !toggle.checked;
    if (typeof options.onModeChange === "function") {
      options.onModeChange(toggle.checked);
    }
  }

  toggle.addEventListener("change", reflectMode);

  return Object.freeze({
    element: section,
    get enabled() {
      return toggle.checked;
    },
    setEnabled(enabled) {
      toggle.checked = Boolean(enabled);
      reflectMode();
    },
    setProgress(update) {
      if (!update || typeof update !== "object") {
        throw new TypeError("progress update must be an object");
      }
      if (update.indeterminate) {
        progress.removeAttribute("value");
      } else if (Number.isInteger(update.percentage)) {
        progress.value = Math.max(0, Math.min(100, update.percentage));
      } else {
        throw new TypeError("determinate progress requires an integer percentage");
      }
      progressText.textContent = String(update.label || "Preparing memory");
      if (update.ready) progress.value = 100;
    },
  });
}
