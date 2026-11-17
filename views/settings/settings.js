const bridge = window.AstrBotPluginView;
const byId = id => document.getElementById(id);
const form = byId("settings-form");
let snapshot = null;
let stats = null;
let busy = false;
const inputs = new Map();

function message(id, text, error = false) {
  byId(id).textContent = text;
  byId(id).dataset.error = String(error);
}

function changes() {
  const result = {};
  for (const field of snapshot?.fields || []) {
    const input = inputs.get(field.path);
    let value = field.type === "bool" ? input.checked : input.value;
    if (["int", "float"].includes(field.type)) {
      if (value.trim() === "" || !Number.isFinite(Number(value))) throw new Error(`请填写「${field.label}」的有效数值。`);
      value = Number(value);
    }
    if (value !== field.value || snapshot.invalid_fields.includes(field.path)
        || field.path === "diagnostics.enabled" && !snapshot.diagnostics_applied) result[field.path] = value;
  }
  return result;
}

function hasDraft() {
  try { return Object.keys(changes()).length > 0; } catch { return true; }
}

function buttons() {
  byId("save").disabled = busy || !snapshot || !hasDraft();
  byId("reload").disabled = busy || !bridge;
  byId("refresh-diagnostics").disabled = busy || !bridge;
  byId("download").disabled = busy || !stats || stats.bytes === 0;
  for (const input of inputs.values()) input.disabled = busy;
}

function diagnostics(value) {
  if (!value || typeof value.enabled !== "boolean"
      || ![value.bytes, value.limit_bytes, value.files].every(v => Number.isSafeInteger(v) && v >= 0)) {
    throw new Error("诊断状态格式异常，请重试。");
  }
  stats = value;
  const size = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1048576
    ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(2)} MB`;
  byId("diagnostics-mode").textContent = value.enabled ? "已开启" : "已关闭";
  byId("diagnostics-capacity").textContent = `${size(value.bytes)} / ${size(value.limit_bytes)}`;
  byId("diagnostics-files").textContent = `${value.files} 个`;
  byId("log-state").textContent = value.bytes === 0
    ? "暂无诊断日志。请保存启用诊断模式，再复现需要排查的问题。"
    : value.enabled ? "正在记录诊断信息。下载不会清除日志。" : "已停止记录，仍可下载之前保留的日志。";
}

function render(value, draft = {}) {
  if (!value || typeof value.revision !== "string" || !Array.isArray(value.fields)) throw new Error("设置响应格式异常，请重试。");
  snapshot = value;
  inputs.clear();
  for (const container of document.querySelectorAll("[data-group]")) {
    container.replaceChildren();
    const group = container.dataset.group;
    if (!["maintenance", "diagnostics"].includes(group)) {
      const heading = document.createElement("h3");
      heading.textContent = value.groups[group]?.label || group;
      container.append(heading);
    }
    if (value.groups[group]?.hint) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = value.groups[group].hint;
      container.append(hint);
    }
    for (const field of value.fields.filter(item => item.group === group)) {
      const row = document.createElement("div");
      row.className = "field";
      row.dataset.pending = String(value.pending_restart.includes(field.path));
      const input = document.createElement(field.options.length ? "select" : "input");
      input.id = `setting-${field.path.replaceAll(".", "-")}`;
      if (field.options.length) {
        field.options.forEach((option, i) => {
          const element = document.createElement("option");
          element.value = option;
          element.textContent = field.labels[i] || option;
          input.append(element);
        });
      } else {
        input.type = field.type === "bool" ? "checkbox" : ["int", "float"].includes(field.type) ? "number" : "text";
        if (input.type === "number") {
          input.step = field.type === "int" ? "1" : "any";
          if (field.bounds) [input.min, input.max] = field.bounds;
        }
      }
      const current = Object.hasOwn(draft, field.path) ? draft[field.path] : field.value;
      if (field.type === "bool") input.checked = current;
      else input.value = current;
      const label = document.createElement("label");
      label.htmlFor = input.id;
      if (field.type === "bool") label.append(input);
      label.append(document.createTextNode(field.label));
      row.append(label);
      if (field.type !== "bool") row.append(input);
      if (field.hint) {
        const hint = document.createElement("p");
        hint.className = "hint";
        hint.id = `${input.id}-hint`;
        hint.textContent = field.hint;
        input.setAttribute("aria-describedby", hint.id);
        row.append(hint);
      }
      inputs.set(field.path, input);
      container.append(row);
    }
  }
  diagnostics(value.diagnostics);
  byId("apply-state").textContent = value.invalid_fields.length
    ? "旧配置包含无效值，已隐藏原值并显示安全默认值。请核对后保存；API 地址需填写有效的 HTTPS 地址。"
    : value.reload_required ? `有 ${value.pending_restart.length} 项已保存设置等待重载生效。请在请求结束后，从 AstrBot 插件页手动重载。`
    : !value.diagnostics_applied ? "诊断开关的已保存值尚未应用，保存一次可同步当前运行状态。" : "当前已保存设置与本次运行一致。";
}

async function load(preserveDraft = true) {
  let draft = {};
  try {
    if (preserveDraft) draft = changes();
    busy = true;
    buttons();
    const value = await bridge.apiGet("settings/config");
    render(value, draft);
    message("save-status", Object.keys(draft).length ? "已读取最新配置并保留草稿，请核对后保存。" : "已读取设置。");
  } catch (error) {
    message("save-status", error.message || "读取设置失败，请确认登录状态后重试。", true);
  } finally { busy = false; buttons(); }
}

form.addEventListener("input", () => {
  message("save-status", hasDraft() ? "有未保存的更改。" : "没有未保存的更改。");
  buttons();
});
form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy || !snapshot) return;
  try {
    const draft = changes();
    if (!Object.keys(draft).length) return;
    busy = true;
    buttons();
    message("save-status", "正在保存…");
    const value = await bridge.apiPost("settings/config", {revision: snapshot.revision, changes: draft});
    render(value);
    message("save-status", value.message);
  } catch (error) {
    // In particular, a 409 never replaces inputs or silently retries a save.
    message("save-status", error.message || "保存失败，草稿已保留。", true);
  } finally { busy = false; buttons(); }
});
byId("reload").addEventListener("click", () => load());
byId("refresh-diagnostics").addEventListener("click", async () => {
  busy = true; buttons();
  try {
    diagnostics(await bridge.apiGet("diagnostics/status"));
    message("download-status", "诊断状态已刷新。");
  } catch { message("download-status", "读取诊断状态失败，请重试。", true); }
  finally { busy = false; buttons(); }
});
byId("download").addEventListener("click", async () => {
  busy = true; buttons();
  try {
    diagnostics(await bridge.apiGet("diagnostics/status"));
    if (!stats.bytes) { message("download-status", "暂无诊断日志，请启用后复现问题。"); return; }
    await bridge.download("diagnostics/download", {}, "ba-stats-diagnostics.zip");
    message("download-status", "下载已交给浏览器。");
  } catch { message("download-status", "下载未完成，请确认登录状态后重试。", true); }
  finally { busy = false; buttons(); }
});
for (const tab of document.querySelectorAll("[data-section]")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll("[data-section]")) {
      if (other === tab) other.setAttribute("aria-current", "page");
      else other.removeAttribute("aria-current");
    }
    for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.id !== tab.dataset.section;
  });
}
window.addEventListener("beforeunload", event => { if (hasDraft()) { event.preventDefault(); event.returnValue = ""; } });

if (bridge) {
  try { await bridge.ready(); await load(false); }
  catch { message("save-status", "无法连接 AstrBot，请重新打开设置中心。", true); }
} else {
  message("save-status", "请从 AstrBot 插件详情 → 可视化视图 → 设置中心打开本页。", true);
}
