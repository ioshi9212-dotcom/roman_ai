(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const pending = new Map();
  let nextId = 1, scene, hostReady = false, busy = false, sent = false;
  let previous = "", selected = new Map(), hostOrigin = "*";

  function notify(method, params) {
    window.parent.postMessage({jsonrpc: "2.0", method, params}, hostOrigin);
  }
  function rpc(method, params) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("HOST_TIMEOUT"));
      }, 15000);
      pending.set(id, {resolve, reject, timer});
      window.parent.postMessage({jsonrpc: "2.0", id, method, params}, hostOrigin);
    });
  }
  function update() {
    $("send").disabled = !scene || !hostReady || busy || sent || !$("draft").value.trim();
    document.querySelectorAll("input[type=checkbox]").forEach(el => el.disabled = busy || sent);
    $("draft").disabled = busy || sent;
  }
  // Keep anchors for untouched selections. Editing a selection turns the edited
  // fragment into free text; later clicks must never overwrite that free text.
  function edited(before, after) {
    let start = 0, end = before.length, newEnd = after.length;
    while (start < end && start < newEnd && before[start] === after[start]) start++;
    while (end > start && newEnd > start && before[end - 1] === after[newEnd - 1]) {end--; newEnd--;}
    const delta = newEnd - end;
    for (const anchor of selected.values()) {
      if (anchor.start == null) continue;
      if (anchor.end <= start) continue;
      if (anchor.start >= end) {anchor.start += delta; anchor.end += delta;}
      else {anchor.start = null; anchor.end = null;}
    }
  }
  $("draft").addEventListener("input", () => {
    edited(previous, $("draft").value);
    previous = $("draft").value;
    update();
  });
  function toggle(item, kind, checked) {
    let text = $("draft").value;
    if (checked) {
      const raw = item.text.trim();
      const wrapped = kind === "speech" ? raw : `(${raw.replace(/^\(([\s\S]*)\)$/, "$1")})`;
      const prefix = text && !/\s$/.test(text) ? " " : "";
      const start = text.length + prefix.length;
      text += prefix + wrapped;
      selected.set(item.id, {start, end: text.length});
    } else {
      const anchor = selected.get(item.id);
      selected.delete(item.id);
      if (anchor?.start != null) {
        let start = anchor.start, end = anchor.end;
        // Only remove a separator adjoining this exact, unedited fragment.
        if (start > 0 && text[start - 1] === " ") start--;
        else if (text[end] === " ") end++;
        const after = text.slice(0, start) + text.slice(end);
        edited(text, after);
        text = after;
      }
    }
    $("draft").value = previous = text;
    update();
  }
  function render(data) {
    if (!data?.groups || !data.session_id || !data.turn_number || !data.scene_sha256) return;
    // Repeated delivery of one result must not destroy a composed draft.
    if (scene?.session_id === data.session_id && scene?.turn_number === data.turn_number && scene?.scene_sha256 === data.scene_sha256) return;
    scene = data; selected = new Map(); previous = ""; sent = false; busy = false;
    $("draft").value = "";
    $("groups").replaceChildren();
    for (const group of data.groups) {
      const field = document.createElement("fieldset"), legend = document.createElement("legend");
      legend.textContent = group.title; field.append(legend);
      for (const item of group.items) {
        const label = document.createElement("label"), box = document.createElement("input"), span = document.createElement("span");
        label.className = "option"; box.type = "checkbox"; box.dataset.id = item.id;
        span.textContent = item.text;
        box.addEventListener("change", () => toggle(item, group.kind, box.checked));
        label.append(box, span); field.append(label);
      }
      $("groups").append(field);
    }
    $("turn").textContent = `Ход ${data.turn_number}`;
    $("status").textContent = "Можно выбрать несколько. Порядок нажатий сохраняется.";
    update();
  }
  function context(value) {
    if (value?.theme) document.documentElement.style.colorScheme = value.theme;
    const inset = value?.safeAreaInsets || {};
    for (const edge of ["top", "right", "bottom", "left"]) {
      document.documentElement.style.setProperty(`--safe-${edge}`, `${Math.max(0, Number(inset[edge]) || 0)}px`);
    }
  }
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.data?.jsonrpc !== "2.0") return;
    if (hostOrigin !== "*" && event.origin !== hostOrigin) return;
    const data = event.data, request = pending.get(data.id);
    if (request) {
      // Sandboxed parents may have an opaque origin; source validation still applies.
      if (event.origin && event.origin !== "null") hostOrigin = event.origin;
      clearTimeout(request.timer); pending.delete(data.id);
      if (data.error) request.reject(new Error(data.error.message || "HOST_ERROR"));
      else request.resolve(data.result);
    } else if (data.method === "ui/notifications/tool-result") {
      if (data.params?.isError) {$("status").textContent = "Не удалось прочитать варианты. Открой панель заново."; return;}
      render(data.params?.structuredContent);
    } else if (data.method === "ui/notifications/host-context-changed") context(data.params);
  });
  $("choices").addEventListener("submit", async event => {
    event.preventDefault();
    if ($("send").disabled) return;
    busy = true; update();
    let messageStarted = false;
    try {
      const result = await rpc("tools/call", {name: "check_scene_choices", arguments: {
        session_id: scene.session_id, expected_turn: scene.turn_number, expected_scene_sha256: scene.scene_sha256,
      }});
      if (result.isError || result.structuredContent?.current !== true) {
        sent = true; throw new Error("SCENE_CHANGED");
      }
      await rpc("ui/update-model-context", {structuredContent: {
        session_id: scene.session_id, based_on_turn: scene.turn_number,
      }});
      messageStarted = true;
      await rpc("ui/message", {role: "user", content: [{type: "text", text: $("draft").value}]});
      sent = true;
      $("status").textContent = "Ход отправлен.";
    } catch (error) {
      if (messageStarted && error.message === "HOST_TIMEOUT") {
        sent = true;
        $("status").textContent = "Подтверждение не пришло. Проверь чат перед повторной отправкой. Текст сохранён.";
      } else {
        $("status").textContent = sent ? "Сцена изменилась. Открой варианты последнего хода. Текст сохранён." : "Не удалось отправить. Текст сохранён, можно повторить.";
      }
    } finally {busy = false; update();}
  });
  async function connect() {
    try {
      const result = await rpc("ui/initialize", {
        appInfo: {name: "Roman AI choices", version: "1.0.0"},
        appCapabilities: {availableDisplayModes: ["inline"]}, protocolVersion: "2026-01-26",
      });
      context(result.hostContext); hostReady = !!result.hostCapabilities?.message;
      notify("ui/notifications/initialized", {});
      if (!hostReady) $("status").textContent = "Этот интерфейс не поддерживает отправку. Скопируй текст в чат.";
      update();
    } catch (_) {$("status").textContent = "Открой панель через подключённый Roman AI в ChatGPT.";}
  }
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => {
    if (hostReady) notify("ui/notifications/size-changed", {height: document.documentElement.scrollHeight});
  }).observe(document.body);
  connect();
})();
