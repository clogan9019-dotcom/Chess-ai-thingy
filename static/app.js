/* Local, dependency-free live dashboard. It polls only this application's API. */
(() => {
  const $ = (id) => document.getElementById(id);
  const board = $("board");
  const toast = $("toast");
  let state = null;
  let flipped = false;
  let lastVersion = -1;
  let toastTimer;

  for (let i = 0; i <= 20; i += 1) {
    const option = document.createElement("option");
    option.value = String(i);
    option.textContent = `${i}${i === 20 ? " (maximum)" : ""}`;
    $("skill-select").append(option);
  }

  function setText(id, text) { $(id).textContent = text == null || text === "" ? "—" : String(text); }
  function time(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }
  function notify(message, error = false) {
    toast.textContent = message;
    toast.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.className = "toast"; }, 3600);
  }
  function playerTurn(s) {
    const turn = s.turn === "white" ? "White" : "Black";
    if (s.state === "opencode_thinking") return "OpenCode is thinking";
    if (s.state === "stockfish_thinking") return "Stockfish is calculating";
    if (s.state === "paused") return "Game paused";
    if (s.state === "error") return "Automation needs attention";
    if (s.outcome.terminal) return s.outcome.label;
    return `${turn} to move — ${s.turn === s.config.ai_color ? "OpenCode" : "Stockfish"}`;
  }
  function boardData(s) {
    if (!flipped) return s.board;
    return [...s.board].reverse();
  }
  function renderBoard(s) {
    board.replaceChildren();
    const move = s.last_move;
    const last = new Set(move ? [move.from, move.to] : []);
    const squares = boardData(s);
    squares.forEach((item, index) => {
      const node = document.createElement("div");
      const file = item.square.charCodeAt(0) - 97;
      const rank = Number(item.square[1]) - 1;
      node.className = `square ${(file + rank) % 2 === 0 ? "light" : "dark"}${last.has(item.square) ? " last" : ""}`;
      const piece = document.createElement("span");
      piece.className = "piece";
      piece.textContent = item.glyph;
      node.append(piece);
      // Board coordinates are drawn only on the outside ranks/files.
      const row = Math.floor(index / 8);
      const col = index % 8;
      if (col === 0) {
        const rankLabel = document.createElement("span");
        rankLabel.className = "rank";
        rankLabel.textContent = item.square[1];
        node.append(rankLabel);
      }
      if (row === 7) {
        const fileLabel = document.createElement("span");
        fileLabel.className = "file";
        fileLabel.textContent = item.square[0];
        node.append(fileLabel);
      }
      board.append(node);
    });
  }
  function renderHistory(history) {
    const target = $("moves");
    target.replaceChildren();
    if (!history.length) {
      const empty = document.createElement("p"); empty.className = "move-empty"; empty.textContent = "No moves yet."; target.append(empty); return;
    }
    const rows = new Map();
    history.forEach((move) => {
      if (!rows.has(move.move_number)) rows.set(move.move_number, []);
      rows.get(move.move_number).push(move);
    });
    [...rows.entries()].forEach(([number, moves]) => {
      const row = document.createElement("div"); row.className = "move-row";
      const numberNode = document.createElement("span"); numberNode.className = "move-no"; numberNode.textContent = `${number}.`;
      row.append(numberNode);
      ["white", "black"].forEach((color) => {
        const move = moves.find((entry) => entry.color === color);
        const node = document.createElement("span");
        node.className = `move-san${move && move.actor === "stockfish" ? " stockfish" : ""}`;
        node.textContent = move ? move.san : "";
        node.title = move ? `${move.actor}: ${move.uci}${move.reasoning ? ` — ${move.reasoning}` : ""}` : "";
        row.append(node);
      });
      target.append(row);
    });
  }
  function renderEvaluation(evaluation) {
    if (!evaluation) {
      setText("eval-text", "±0.00"); $("eval-fill").style.width = "50%"; return;
    }
    setText("eval-text", evaluation.display || "?");
    let percentage = 50;
    if (evaluation.type === "centipawn") percentage = Math.max(3, Math.min(97, 50 + (evaluation.value / 20)));
    else if (evaluation.type === "mate") percentage = evaluation.value > 0 ? 97 : 3;
    $("eval-fill").style.width = `${percentage}%`;
    $("eval-note").textContent = evaluation.pv?.length ? `Last Stockfish principal variation: ${evaluation.pv.join(" ")}` : "Stockfish returned this score while choosing its own move.";
  }
  function renderThinking(s) {
    const lastOpenCodeMove = [...s.history].reverse().find((m) => m.actor === "opencode");
    const thinking = s.state === "opencode_thinking";
    $("thinking-box");
    document.querySelector(".pulse").classList.toggle("active", thinking);
    setText("thinking-time", s.opencode?.elapsed_seconds != null ? `${s.opencode.elapsed_seconds}s` : thinking ? "working" : "—");
    $("reasoning").textContent = lastOpenCodeMove?.reasoning || (thinking ? "OpenCode is using the Chess MCP board and legal-moves tools." : "OpenCode reasoning will be saved here after its first submitted move.");
    const candidates = $("candidates"); candidates.replaceChildren();
    (lastOpenCodeMove?.candidates || []).forEach((candidate) => { const el = document.createElement("span"); el.className = "candidate"; el.textContent = candidate; candidates.append(el); });
    const usage = s.opencode?.token_usage;
    $("token-usage").textContent = usage ? `Tokens: ${usage.total ?? "?"} total · ${usage.input ?? "?"} input · ${usage.output ?? "?"} output` : "Token usage is shown when exposed by your OpenCode CLI.";
  }
  function renderActivity(s) {
    const lines = [`[${s.state}] ${s.message}`];
    if (s.last_error) lines.push(`ERROR: ${s.last_error}`);
    if (s.opencode?.error) lines.push(`OpenCode: ${s.opencode.error}`);
    if (s.opencode?.output_tail) lines.push("\nOpenCode CLI tail:\n" + s.opencode.output_tail);
    $("activity").textContent = lines.join("\n");
    const badge = $("state-badge"); badge.textContent = s.state.replaceAll("_", " "); badge.className = `state-badge ${s.state}`;
  }
  function render(s) {
    state = s;
    $("connection").className = "connection live"; $("connection").innerHTML = "<i></i> Local runner online";
    setText("now-playing", playerTurn(s));
    setText("status-message", s.message);
    setText("result", s.outcome.result);
    setText("check-status", s.in_check ? "Check" : s.outcome.label);
    setText("elapsed", time(s.elapsed_seconds));
    setText("move-count", `${s.history.length} ${s.history.length === 1 ? "ply" : "plies"}`);
    setText("stockfish-color", `${s.config.stockfish_color.toUpperCase()} / ENGINE`);
    setText("stockfish-detail", `Skill ${s.config.stockfish_skill_level} · ${s.config.stockfish_move_time_seconds}s / move`);
    setText("opencode-color", `${s.config.ai_color.toUpperCase()} / AI`);
    setText("model-name", s.config.opencode_model);
    $("color-select").value = s.config.ai_color;
    $("skill-select").value = String(s.config.stockfish_skill_level);
    $("captured-white").textContent = s.captured.white.join(" ") || "—";
    $("captured-black").textContent = s.captured.black.join(" ") || "—";
    $("fen-short").textContent = `FEN: ${s.fen}`;
    setText("history-count", `${Math.ceil(s.history.length / 2)} moves`);
    renderBoard(s); renderHistory(s.history); renderEvaluation(s.evaluation); renderThinking(s); renderActivity(s);
  }
  async function request(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    let body; try { body = await response.json(); } catch { body = null; }
    if (!response.ok) throw new Error(body?.detail || `Request failed (${response.status})`);
    return body;
  }
  async function action(path, body) {
    try { const next = await request(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }); if (next.state) render(next); notify("Control action accepted."); }
    catch (error) { notify(error.message, true); }
  }
  $("new-game").addEventListener("click", () => action("/api/new-game", { ai_color: $("color-select").value, stockfish_skill_level: Number($("skill-select").value) }));
  $("pause").addEventListener("click", () => action("/api/pause"));
  $("resume").addEventListener("click", () => action("/api/resume"));
  $("undo").addEventListener("click", () => action("/api/undo", { plies: 1 }));
  $("resign").addEventListener("click", () => { if (confirm("End the match by resigning OpenCode?")) action("/api/resign", { reason: "Resigned from dashboard" }); });
  $("flip-board").addEventListener("click", () => { flipped = !flipped; if (state) renderBoard(state); });
  async function poll() {
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error("runner unavailable");
      const next = await response.json();
      if (next.version !== lastVersion || !state) { lastVersion = next.version; render(next); }
      else { setText("elapsed", time(next.elapsed_seconds)); }
    } catch (error) {
      $("connection").className = "connection dead"; $("connection").innerHTML = "<i></i> Runner unavailable";
    } finally { setTimeout(poll, 700); }
  }
  poll();
})();
