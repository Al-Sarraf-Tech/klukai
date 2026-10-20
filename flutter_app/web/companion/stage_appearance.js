// Appearance catalog, transactional model replacement and portrait/3D switching.
// The app retains avatar ownership through explicit live accessors on runtime.
import { createAppearanceSwitcher } from './appearance.js';
import { moodToExpression } from './logic.js';

// `framing`/`viewKey`: the embedded view frames her waist-up and keeps its
// own saved camera (prefs.embedView), so a full-body view saved on the full
// stage never overrides it; `doubleTapReset` eases back to that framing.
export function createStageAppearance({ $, api, prefs, savePrefs, notice, portrait, stage,
  runtime, syncButtons, syncInsets, framing = "full", viewKey = "view", doubleTapReset = false }) {
  // ── Portrait ⇄ 3D window ─────────────────────────────────────────────────
  let models = [];
  let requestedAppearance = prefs.appearance || "default";
  const appearances = createAppearanceSwitcher({
    async build(id, signal) {
      const { createAvatar3D } = await import("./avatar3d.js?v=297fb50b742a");
      const host = document.createElement("div");
      host.className = "model-view";
      host.style.visibility = "hidden";
      $("model").appendChild(host);
      signal.addEventListener("abort", () => host.remove(), { once: true });
      let error, candidate;
      try {
        const av = await createAvatar3D(host, {
          framing,
          view: prefs[viewKey],
          doubleTapReset,
          onViewChange: (view) => {
            if (runtime.avatar !== candidate) return;
            prefs[viewKey] = view;
            savePrefs(prefs);
          },
          modelKey: id, signal, loadModel: () => api.model(id, { signal }),
          onError: (e) => { error = e; },
        });
        candidate = av;
        const dispose = av.dispose.bind(av);
        av.dispose = () => { dispose(); host.remove(); };
        if (!av.ready) { av.dispose(); throw error || new Error("3D unavailable"); }
        av.reveal = () => { host.style.visibility = ""; };
        return av;
      } catch (e) { host.remove(); throw e; }
    },
    commit(av, id) {
      // A user may move the current model while the replacement is loading.
      av.setView(runtime.avatar?.getView() ?? prefs[viewKey]);
      av.reveal();
      runtime.avatar = av;
      prefs[viewKey] = av.getView();
      prefs.appearance = id;
      savePrefs(prefs);
      $("appearance-select").value = id;
      renderAppearanceCredit(id);
      runtime.avatar.setTint?.(moodToExpression(runtime.state.mood));
      runtime.avatar.setMood(runtime.state.mood);
      if (runtime.state.thinking) runtime.avatar.setState("thinking");
      runtime.avatar.setPaused(prefs.mode !== "model" || runtime.hidden);
      syncInsets();
      notice("");
    },
  });

  function renderAppearanceCredit(id) {
    const entry = models.find((m) => m.id === id);
    $("appearance-credit").textContent = entry ? `${entry.creator} · ${entry.license}` : "";
    $("appearance-description").textContent = entry?.description || "";
    const link = $("appearance-source");
    const url = entry?.source || "";
    link.hidden = !url.startsWith("https://");
    if (!link.hidden) link.href = url;
    else link.removeAttribute("href");
  }

  async function refreshAppearances() {
    try {
      const result = await api.models();
      models = result.models;
      const select = $("appearance-select");
      select.replaceChildren(...models.map((m) => {
        const option = document.createElement("option");
        option.value = m.id;
        option.textContent = m.name + (m.available ? "" : " · Not installed");
        option.disabled = !m.available;
        return option;
      }));
      select.disabled = false;
      if (!models.some((m) => m.id === prefs.appearance && m.available)) {
        const changed = prefs.appearance !== "default";
        prefs.appearance = "default";
        requestedAppearance = "default";
        savePrefs(prefs);
        if (changed && runtime.awake && prefs.mode === "model") await setMode("model");
      }
      select.value = prefs.appearance;
      renderAppearanceCredit(prefs.appearance);
    } catch { /* keep the pinned default usable if the catalog is temporarily unavailable */ }
  }

  async function setMode(mode, appearance = requestedAppearance) {
    requestedAppearance = appearance;
    prefs.mode = mode;
    savePrefs(prefs);
    stage.dataset.mode = mode;
    const model = $("model");
    if (mode === "model") {
      portrait.setActive(false);
      model.hidden = false;
      if (appearances.key !== appearance) notice("Loading her 3D model…", 0);
      try {
        const applied = await appearances.select(appearance);
        if (!applied) return;
        notice("");
      } catch (e) {
        requestedAppearance = appearances.key || prefs.appearance;
        $("appearance-select").value = appearances.key || prefs.appearance;
        notice(e?.status === 404 ? "That appearance isn't installed yet." : "Couldn't load that appearance. Try again.", 6000);
        if (!runtime.avatar) {
          prefs.mode = "portrait";
          savePrefs(prefs);
          stage.dataset.mode = "portrait";
          model.hidden = true;
          portrait.setActive(runtime.awake);
        }
      }
      syncInsets();
    } else {
      model.hidden = true;
      portrait.setActive(runtime.awake);
    }
    runtime.avatar?.setPaused(prefs.mode !== "model" || runtime.hidden);
    syncButtons();
    syncInsets();
  }

  $("btn-appearance").addEventListener("click", () => {
    $("appearance-dialog").showModal();
    refreshAppearances();
  });
  $("appearance-select").addEventListener("change", (event) => {
    const id = event.target.value;
    if (!models.some((m) => m.id === id && m.available)) return;
    $("appearance-dialog").close();
    setMode("model", id);
  });

  return { refreshAppearances, setMode };
}
