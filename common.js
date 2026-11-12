const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtDate = s => s.split("-").reverse().join(".");
const fmtStamp = s => `${s.slice(8, 10)}.${s.slice(5, 7)} ${s.slice(11)}`;
const plural = (n, f) => n % 10 === 1 && n % 100 !== 11 ? f[0] : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? f[1] : f[2];
/* Api(["adm","sup"], onAuthFail): токен берётся из первого непустого ключа localStorage */
function Api(keys, onFail) {
  return async function (path, method = "GET", body) {
    const t = keys.map(k => localStorage.getItem(k)).find(Boolean) || "";
    const r = await fetch(path, {method, headers: {"Content-Type": "application/json", Authorization: "Bearer " + t}, body: body ? JSON.stringify(body) : undefined});
    const d = await r.json().catch(() => ({}));
    if (r.status === 401 && !path.endsWith("/login")) { keys.forEach(k => localStorage.removeItem(k)); onFail(); throw new Error("Нужен вход."); }
    if (!r.ok) throw new Error(d.error || "Ошибка");
    return d;
  };
}
