"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

const LABELS = {
  stage: { greeting: "인사", ask_number: "번호 요청", need_area_code: "지역번호 확인", readback: "번호 재확인", ask_other: "다른 문의처", ended: "종료" },
  role: { management_office: "관리사무소", real_estate: "부동산", security_office: "경비실", building_owner: "건물주", unknown: "용도 미확인" },
  status: { candidate: "후보", confirmed_by_source: "상대방 재확인 완료", rejected: "상대가 부정", superseded: "정정됨" },
  outcome: {
    number_confirmed_by_source: "번호 확인 완료", candidate_unconfirmed: "후보만 확보(미확인)", referred_other_contact: "다른 문의처 소개",
    not_found: "미확보", refused: "거절", callback_requested: "재통화 요청", wrong_contact: "잘못된 문의처", no_response: "응답 없음",
    incomplete: "미완료", hangup_before_result: "결과 전 종료", not_connected: "연결 안 됨",
  },
};
const L = (group, key) => LABELS[group][key] || key || "-";

let CONFIG = {};
let current = null; // { callId, ended }
let playing = null; // { responseId, audio }

// ---------------------------------------------------------------- tabs
$$(".tabs button").forEach((b) => b.addEventListener("click", () => {
  $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "results") loadResults();
  if (b.dataset.tab === "template") loadTemplate();
  if (b.dataset.tab === "voice") loadVoices();
}));

// -------------------------------------------------------------- config
async function loadConfig() {
  CONFIG = await api("/api/config");
  $("#providers").innerHTML =
    `LLM <b>${esc(CONFIG.llm_provider)}</b> · TTS <b>${esc(CONFIG.tts_provider)}</b> · ` +
    `전화 <b>${CONFIG.twilio_enabled ? "Twilio" : "시뮬레이터만"}</b>`;
  $("#tpl-vars").textContent = CONFIG.template_variables.map((v) => `{${v}}`).join(" ");
  $("#voice-hint").textContent = CONFIG.tts_can_clone
    ? "현재 TTS 공급사에서 목소리 복제를 지원합니다. 등록하면 voice_id가 발급됩니다."
    : `현재 TTS(${CONFIG.tts_provider})는 목소리 복제를 지원하지 않습니다. 녹음 파일은 저장만 되고, 통화는 기본 음성으로 합니다. ` +
      "본인 목소리를 쓰려면 TTS_PROVIDER=elevenlabs 와 ELEVENLABS_API_KEY 를 설정하세요 (유료).";
}

// ---------------------------------------------------------------- jobs
$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  if (!data.voice_id) delete data.voice_id;
  try { await api("/api/call-jobs", { json: data }); loadJobs(); } catch (err) { alert(err.message); }
});

async function loadJobs() {
  const jobs = await api("/api/call-jobs");
  $("#jobs").innerHTML = jobs.map((j) => `
    <div class="job">
      <div><b>${esc(j.building_name)}</b> ← ${esc(j.recipient_name || "")} ${esc(j.recipient_number)}</div>
      <div class="muted">상태: <span class="status-${esc(j.status)}">${esc(j.status)}</span> · 시도 ${j.attempts}회</div>
      <div class="row">
        <button data-start="${j.id}" data-mode="simulator">시뮬레이터 통화</button>
        <button data-start="${j.id}" data-mode="twilio" ${CONFIG.twilio_enabled ? "" : "disabled title='Twilio 미설정'"}>실제 전화</button>
        <button data-cancel="${j.id}" class="danger">중단</button>
      </div>
    </div>`).join("") || `<p class="hint">작업이 없습니다.</p>`;
}

$("#jobs").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.start) {
      const res = await api(`/api/call-jobs/${b.dataset.start}/start`, { json: { mode: b.dataset.mode } });
      if (b.dataset.mode === "twilio") {
        alert("발신을 요청했습니다. 진행 상황은 결과 탭에서 확인하세요.");
      } else {
        startCall(res);
      }
    } else if (b.dataset.cancel) {
      await api(`/api/call-jobs/${b.dataset.cancel}/cancel`, { method: "POST" });
    }
  } catch (err) { alert(err.message); }
  loadJobs();
});

// ---------------------------------------------------------------- call
function setInputs(on) {
  $$("#say-form input, #say-form button, #btn-silence, #btn-hangup").forEach((el) => (el.disabled = !on));
  $("#mic").disabled = !on || !Recognition;
}

function startCall(res) {
  current = { callId: res.call.id, ended: false };
  setInputs(true);
  handle(res);
  $("#say-input").focus();
}

function render(state) {
  $("#call-stage").textContent = L("stage", state.stage);
  const chat = $("#chat");
  chat.innerHTML = state.utterances.map((u) => `
    <div class="msg ${u.speaker} ${u.status === "interrupted" ? "interrupted" : ""}">
      <div class="meta">${u.id} · ${u.speaker === "bot" ? "봇" + (u.kind ? " · " + esc(u.kind) : "") : "상대" + (u.intent ? " · " + esc(u.intent) + " (" + esc(u.source) + ")" : "")}${u.status === "interrupted" ? " · 중단됨" : ""}</div>
      ${esc(u.text)}
    </div>`).join("") + (state.stage === "ended"
      ? `<div class="msg system">통화 종료 · 결과: <b>${esc(L("outcome", state.outcome))}</b>${state.do_not_retry ? " · 재발신 제외" : ""}</div>` : "");
  chat.scrollTop = chat.scrollHeight;
  $("#contacts").innerHTML = state.contacts.map(contactHtml).join("") || `<p class="hint">아직 없습니다.</p>`;
}

function contactHtml(c) {
  const ev = [...(c.provided_in || []).map((x) => `제공 ${x}`), c.readback_in && `낭독 ${c.readback_in}`, c.confirmed_in && `확인 ${c.confirmed_in}`].filter(Boolean);
  return `<div class="contact">
    <span class="num">${esc(c.formatted || c.digits)}</span>
    ${c.complete ? "" : '<span class="muted">(불완전)</span>'}
    · ${esc(L("role", c.role))} · <span class="st-${esc(c.confirmation_status)}">${esc(L("status", c.confirmation_status))}</span>
    <div class="muted">원문: ${esc(c.raw_text)} ${ev.map((x) => `<span class="evidence">${esc(x)}</span>`).join("")}</div>
  </div>`;
}

function handle(res) {
  render(res.state);
  if (res.turn) speak(res.turn);
  if (res.state.stage === "ended") {
    current.ended = true;
    setInputs(false);
    stopRecognition();
    loadJobs();
  }
}

async function send(text, how = "typed") {
  if (!current || current.ended || !text.trim()) return;
  bargeIn(how);
  try { handle(await api(`/api/calls/${current.callId}/utterance`, { json: { text } })); } catch (err) { alert(err.message); }
}

$("#say-form").addEventListener("submit", (e) => { e.preventDefault(); const i = $("#say-input"); send(i.value); i.value = ""; });
$$(".quick .q").forEach((b) => b.addEventListener("click", () => send(b.textContent)));
$("#btn-silence").addEventListener("click", async () => { if (current) handle(await api(`/api/calls/${current.callId}/silence`, { method: "POST" })); });
$("#btn-hangup").addEventListener("click", async () => { if (current) { bargeIn("voice"); handle(await api(`/api/calls/${current.callId}/hangup`, { method: "POST" })); } });

// ------------------------------------------------------- speaking / barge-in
async function speak(turn) {
  bargeIn("silent");
  if (!$("#speak").checked) return;
  const done = () => {
    if (playing && playing.responseId === turn.response_id) {
      playing = null;
      api(`/api/calls/${current.callId}/played`, { json: { response_id: turn.response_id } }).catch(() => {});
    }
  };
  playing = { responseId: turn.response_id, audio: null };
  if (turn.audio_url) {
    const r = await fetch(turn.audio_url);
    if (r.status === 200 && playing && playing.responseId === turn.response_id) {
      const audio = new Audio(URL.createObjectURL(await r.blob()));
      playing.audio = audio;
      audio.onended = done;
      audio.play().catch(done);
      return;
    }
  }
  if ("speechSynthesis" in window) {
    const u = new SpeechSynthesisUtterance(turn.text);
    u.lang = "ko-KR";
    u.onend = done;
    speechSynthesis.speak(u);
  }
}

// 상대가 말하기 시작하면: 재생 중단 → 서버에 중단 기록 (늦은 음성은 response_id로 무시)
//  how = "voice"  : 마이크로 말함 → 끝까지 못 들은 것으로 기록 (재확인 문장이면 다시 읽음)
//  how = "typed"  : 텍스트 입력 → 화면에서 전체 문장을 봤으므로 재생 완료로 기록
//  how = "silent" : 새 봇 문장 재생 전 정리
function bargeIn(how) {
  if (!playing) return;
  const p = playing;
  playing = null;
  if (p.audio) p.audio.pause();
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (!current || how === "silent") return;
  const action = how === "voice" ? "interrupt" : "played";
  api(`/api/calls/${current.callId}/${action}`, { json: { response_id: p.responseId } }).catch(() => {});
}

// ----------------------------------------------------- browser microphone (free STT)
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
function stopRecognition() { if (rec) { rec.onend = null; rec.stop(); rec = null; $("#mic").classList.remove("primary"); } }
$("#mic").addEventListener("click", () => {
  if (rec) return stopRecognition();
  rec = new Recognition();
  rec.lang = "ko-KR";
  rec.continuous = true;
  rec.interimResults = true;
  rec.onresult = (e) => {
    bargeIn("voice"); // 중간 결과가 나오면 곧바로 봇 음성을 멈춘다
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) send(e.results[i][0].transcript, "voice");
    }
  };
  rec.onend = () => { if (rec && current && !current.ended) rec.start(); };
  rec.start();
  $("#mic").classList.add("primary");
});

// -------------------------------------------------------------- results
async function loadResults() {
  const calls = await api("/api/calls");
  $("#results tbody").innerHTML = calls.map((c) => {
    const live = c.contacts.filter((x) => x.confirmation_status !== "superseded");
    const best = live.find((x) => x.confirmation_status === "confirmed_by_source") || live[0];
    return `<tr class="clickable" data-call="${c.id}">
      <td>${new Date(c.started_at * 1000).toLocaleString("ko-KR")}</td>
      <td>${esc(c.building_name)}</td>
      <td>${esc(c.recipient_name || "")}<div class="muted">${esc(c.recipient_number)} · ${esc(c.mode)}</div></td>
      <td>${best ? `<b>${esc(best.formatted || best.digits)}</b><div class="muted">${esc(L("role", best.role))}${live.length > 1 ? ` 외 ${live.length - 1}건` : ""}</div>` : "-"}</td>
      <td class="st-${best ? esc(best.confirmation_status) : ""}">${best ? esc(L("status", best.confirmation_status)) : "-"}</td>
      <td class="oc-${esc(c.outcome)}">${esc(c.status === "in_progress" ? "진행 중" : L("outcome", c.outcome))}</td>
      <td>${esc(c.review_status)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="hint">통화 기록이 없습니다.</td></tr>`;
}

$("#results").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr[data-call]");
  if (tr) showDetail(tr.dataset.call);
});

async function showDetail(id) {
  const d = await api(`/api/calls/${id}`);
  const evidence = new Set(d.contacts.flatMap((c) => [...c.provided_in, c.readback_in, c.confirmed_in]).filter(Boolean));
  $("#detail").hidden = false;
  $("#detail-id").textContent = `${d.call_id} · ${d.template_version} · 해석기 ${d.interpreter}`;
  $("#detail-body").innerHTML = `
    <p>결과: <b class="oc-${esc(d.outcome)}">${esc(L("outcome", d.outcome))}</b> · 종료 사유: ${esc(d.end_reason)}
      ${d.callback_request ? ` · 재통화 요청: ${esc(d.callback_request)}` : ""}${d.do_not_retry ? " · 재발신 제외" : ""}</p>
    ${d.contacts.map(contactHtml).join("")}
    <div class="chat" style="height:auto;max-height:420px">${d.utterances.map((u) => `
      <div class="msg ${u.speaker} ${u.status === "interrupted" ? "interrupted" : ""}">
        <div class="meta">${u.id} · ${u.ts}s ${evidence.has(u.id) ? '<span class="evidence">근거</span>' : ""}</div>${esc(u.text)}
      </div>`).join("")}</div>
    <div class="row">사람 검토:
      <button data-review="approved">승인</button><button data-review="rejected" class="danger">반려</button>
      <span class="muted">현재: ${esc(d.review_status)}</span></div>`;
  $$("#detail-body [data-review]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/calls/${id}/review`, { json: { review_status: b.dataset.review } });
    showDetail(id); loadResults();
  }));
}

// ------------------------------------------------------------- template
async function loadTemplate() {
  const t = await api("/api/templates/latest");
  $("#tpl-version").textContent = `v${t.version}`;
  $("#tpl-persona").value = t.content.persona_prompt;
  $("#tpl-phrases").innerHTML = Object.entries(t.content.phrases).map(([k, v]) =>
    `<label>${esc(k)} <input data-phrase="${esc(k)}" value="${esc(v)}"></label>`).join("");
}

$("#tpl-save").addEventListener("click", async () => {
  const phrases = Object.fromEntries($$("[data-phrase]").map((i) => [i.dataset.phrase, i.value]));
  try {
    const t = await api("/api/templates", { json: { content: { persona_prompt: $("#tpl-persona").value, phrases } } });
    $("#tpl-msg").textContent = ` v${t.version} 저장됨`;
    loadTemplate();
  } catch (err) { $("#tpl-msg").textContent = " " + err.message; }
});

// ---------------------------------------------------------------- voice
async function loadVoices() {
  const voices = await api("/api/voices");
  $("#voices").innerHTML = voices.map((v) => `
    <div class="job"><b>${esc(v.name)}</b> <span class="muted">${esc(v.provider)} · voice_id: ${esc(v.provider_voice_id || "(복제 미지원 - 기본 음성)")}</span>
      <div class="row"><button data-preview="${v.id}">미리 듣기</button><button data-del="${v.id}" class="danger">삭제</button></div></div>`).join("")
    || `<p class="hint">등록된 목소리가 없습니다.</p>`;
  $("#job-voice").innerHTML = `<option value="">기본 음성</option>` +
    voices.map((v) => `<option value="${v.id}">${esc(v.name)}</option>`).join("");
}

$("#voices").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.del) { await api(`/api/voices/${b.dataset.del}`, { method: "DELETE" }); return loadVoices(); }
  const text = $("#preview-text").value;
  const r = await fetch(`/api/voices/${b.dataset.preview}/preview`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
  });
  if (r.status === 200) new Audio(URL.createObjectURL(await r.blob())).play();
  else { const u = new SpeechSynthesisUtterance(text); u.lang = "ko-KR"; speechSynthesis.speak(u); }
});

$("#voice-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/voices", { method: "POST", body: new FormData(e.target) });
    e.target.reset();
    loadVoices();
  } catch (err) { alert(err.message); }
});

// ----------------------------------------------------------------- init
loadConfig().then(() => { loadJobs(); loadVoices(); });
