/* Focus save bridge for ONLYOFFICE Docs Community Edition.
   Scoped polling delivers force-save commands; editor events track unsaved changes. */
(function () {
  "use strict";

  const pathParts = location.pathname.split("/").filter(Boolean);
  const configIndex = pathParts.lastIndexOf("plugin-config");
  const bridgeOrigin = location.origin;
  const sessionId = configIndex >= 0 ? decodeURIComponent(pathParts[configIndex + 1] || "") : "";
  const accessToken = configIndex >= 0 ? decodeURIComponent(pathParts[configIndex + 3] || "") : "";
  let documentVersion = 1;
  let polling = false;
  let stopped = false;

  function endpoint(path) {
    const separator = path.includes("?") ? "&" : "?";
    return `${bridgeOrigin}${path}${separator}access_token=${encodeURIComponent(accessToken)}`;
  }

  async function send(path, body) {
    const response = await fetch(endpoint(path), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Focus Bridge HTTP ${response.status}`);
    return response.json();
  }

  async function handle(command) {
    if (command.action !== "force_save") {
      return { changed: false, document_version: documentVersion, error: "Unsupported editor command" };
    }
    Asc.scope.focusCommand = command;
    return new Promise(resolve => window.Asc.plugin.callCommand(function () {
      const command = Asc.scope.focusCommand;
      try {
        if (typeof Api.Save !== "function") throw new Error("Editor save API is unavailable");
        Api.Save();
        return { changed: false, document_version: command.expected_version, evidence: { force_save_requested: true } };
      } catch (error) {
        return { changed: false, document_version: command.expected_version, error: String(error?.message || error) };
      }
    }, false, true, resolve));
  }

  async function poll() {
    if (polling || stopped || !bridgeOrigin || !sessionId || !accessToken) return;
    polling = true;
    while (!stopped) {
      try {
        const response = await fetch(endpoint(`/bridge/commands/${encodeURIComponent(sessionId)}/next?timeout=25`));
        if (!response.ok) throw new Error(`poll HTTP ${response.status}`);
        const { command } = await response.json();
        if (!command || stopped) continue;
        const result = await handle(command);
        await send(`/bridge/commands/${encodeURIComponent(sessionId)}/${encodeURIComponent(command.command_id)}/result`, result);
      } catch {
        if (!stopped) await new Promise(resolve => setTimeout(resolve, 1000));
      }
    }
    polling = false;
  }

  window.Asc.plugin.init = function () {
    send(`/bridge/events/${encodeURIComponent(sessionId)}`, {
      event: "editorReady", document_version: documentVersion, data: {},
    }).catch(() => {});
    if (typeof window.Asc.plugin.attachEditorEvent === "function") {
      window.Asc.plugin.attachEditorEvent("onDocumentContentChanged", window.Asc.plugin.event_onDocumentContentChanged);
    }
    poll();
  };
  window.Asc.plugin.event_onDocumentContentChanged = function () {
    documentVersion += 1;
    send(`/bridge/events/${encodeURIComponent(sessionId)}`, {
      event: "documentChanged", document_version: documentVersion, data: {},
    }).catch(() => {});
  };
  window.addEventListener("beforeunload", () => { stopped = true; });
})();
