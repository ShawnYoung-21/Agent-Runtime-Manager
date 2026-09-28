"""arm ui：本地 Web 控制台（零新增依赖，仅标准库 http.server）。

提供：
- GET  /            控制台页面（状态卡、protect/release 按钮、事件时间线、daemon 心跳）
- GET  /api/state   JSON：保护状态(有效值+原始值)、环境、agent 列表、daemon 心跳
- POST /api/protect / /api/release   进入/释放保护
- GET  /api/events  最近事件流水（含时间戳，用于合盖冻结判读）

设计：只读为主 + 两个显式动作按钮；单文件内嵌 HTML；端口 127.0.0.1 仅本机。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from arm.core.store import Store
from arm.sensors import power as power_sensor
from arm.sensors import standby as standby_sensor

_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARM 控制台</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0d1015; --side: #0a0c10; --panel: #161a22; --panel2: #1a1f29;
    --border: #242a36; --border2: #2e3542;
    --txt: #f0f2f5; --txt2: #a8b0bd; --txt3: #6b7280;
    --claude: #38bdf8; --codex: #facc15; --acc: #6480f3;
    --ok: #4ade80; --warn: #facc15; --bad: #f87171;
  }
  * { box-sizing: border-box; }
  /* hidden 属性必须赢过任何作者 display 规则——否则 .kpi .stale 的 display:block
     会让"守护已停止"红字一旦闪现就永久粘在界面（2026-09-28 实测） */
  [hidden] { display: none !important; }
  html, body { height: 100%; }
  body { margin:0; font: 14px/1.6 "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
         background: var(--bg); color: var(--txt); display: flex; overflow: hidden;
         -webkit-font-smoothing: antialiased; }

  /* ===== 侧栏：实体面板 ===== */
  .side { width: 200px; flex: none; background: var(--side);
          border-right: 1px solid var(--border); display:flex; flex-direction: column;
          padding: 22px 14px 16px; }
  .brand { padding: 0 10px 24px; }
  .brand .logo { display:flex; align-items:center; gap:9px; }
  .brand .logo img { width: 26px; height: 26px; border-radius: 6px; }
  .brand .t { font-size: 16px; font-weight: 800; letter-spacing: .08em; }
  .brand .s { color: var(--txt3); font-size: 10.5px; margin-top: 3px; letter-spacing: .02em; }
  nav { flex: 1; display:flex; flex-direction:column; gap:2px; }
  .nav-item { display:flex; align-items:center; gap:11px; padding:9px 13px; border-radius:9px;
              color:var(--txt2); cursor:pointer; font-size:13.5px; border:none;
              background:none; text-align:left; width:100%; transition: all .14s; }
  .nav-item:hover { color:var(--txt); background:rgba(255,255,255,.05); }
  .nav-item.on { color:#fff; background:var(--panel2); font-weight:650;
                 box-shadow: inset 0 0 0 1px var(--border2); }
  .nav-item.on .ic { color: var(--acc); }
  .nav-item .ic { width:17px; text-align:center; font-size:14px; }
  .side .foot { padding-top: 14px; border-top: 1px solid var(--border); }
  .guard-btn { width:100%; padding:10px; border-radius:9px; font-weight:700; font-size:13px;
               cursor:pointer; border:none; transition: all .15s; margin-bottom:8px; }
  .guard-on { background:rgba(74,222,128,.16); color:var(--ok); }
  .guard-off { background:rgba(248,113,113,.14); color:var(--bad); }

  /* ===== 主区 ===== */
  .main { flex:1; display:flex; flex-direction:column; min-width:0; }
  .topbar { display:flex; align-items:center; gap:16px; padding: 18px 30px 16px; }
  .topbar .title { font-size:21px; font-weight:750; letter-spacing:-.01em; }
  .hbadge { display:inline-flex; align-items:center; gap:8px; padding:5px 14px; border-radius:99px;
            font-size:12.5px; font-weight:600; border:1px solid; }
  .hbadge .d { width:7px; height:7px; border-radius:50%; background:currentColor;
               animation: pulse 2s infinite; }
  .hb-ok   { background:rgba(74,222,128,.1); border-color:rgba(74,222,128,.25); color:var(--ok); }
  .hb-warn { background:rgba(250,204,21,.07); border-color:rgba(250,204,21,.25); color:var(--warn); }
  .hb-bad  { background:rgba(248,113,113,.07); border-color:rgba(248,113,113,.25); color:var(--bad); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .topbar .right { margin-left:auto; color:var(--txt3); font-size:12px; }
  .view { flex:1; overflow-y:auto; padding: 4px 30px 30px; }
  .page { display:none; }
  .page.on { display:block; }
  .ph { font-size:15px; font-weight:700; color:var(--txt2); margin:0 0 14px; }
  .pd { color:var(--txt3); font-size:12.5px; margin-bottom:20px; max-width:680px; }

  /* ===== Agent 大卡：顶部色带分层 ===== */
  .acards { display:grid; grid-template-columns: repeat(auto-fill,minmax(330px,1fr)); gap:16px; }
  .acard { background: var(--panel); border:1px solid var(--border); border-radius:14px;
           overflow:hidden; transition: border-color .15s; }
  .acard:hover { border-color: var(--border2); }
  .acard .band { height: 3px; background: var(--claude); }
  .acard.codex .band { background: var(--codex); }
  .acard .inner { padding: 16px 18px 15px; }
  .acard .nm { font-size:16px; font-weight:750; line-height:1.35; word-break:break-all; }
  .acard .tagline { display:flex; align-items:center; gap:10px; margin-top:8px;
                    color:var(--txt2); font-size:12.5px; }
  .atag { font-size:11px; font-weight:700; letter-spacing:.05em; padding:2px 9px; border-radius:6px; }
  .atag.claude-cli { background:rgba(56,189,248,.13); color:var(--claude); }
  .atag.claude-desktop { background:rgba(129,140,248,.13); color:#a5b4fc; }
  .atag.codex-cli { background:rgba(250,204,21,.11); color:var(--codex); }
  .atag.codex-desktop { background:rgba(251,146,60,.13); color:#fdba74; }
  .acard .meta-row { display:flex; align-items:center; gap:7px; margin-top:14px; flex-wrap:wrap; }

  /* KPI：三格卡片 */
  .kpis { display:grid; grid-template-columns: repeat(auto-fit,minmax(200px,1fr)); gap:12px;
          margin-bottom: 8px; }
  .kpi { background:var(--panel); border:1px solid var(--border); border-radius:13px;
         padding:14px 17px; }
  .kpi .k { color:var(--txt3); font-size:11px; font-weight:700; letter-spacing:.09em;
            text-transform:uppercase; display:block; }
  .kpi .v { font-size:17px; font-weight:700; margin-top:8px; display:block; }
  .kpi .n { font-size:12px; color:var(--txt2); margin-top:5px; min-height:16px; display:block; }
  .kpi .stale { display:block; margin-top:4px; }
  .kpi .warn-t { display:block; margin-top:4px; }
  .badge { display:inline-block; padding:2px 10px; border-radius:99px; font-size:12.5px; font-weight:700; }
  .b-protect { background:rgba(74,222,128,.14); color:var(--ok); }
  .b-armed { background:rgba(250,204,21,.13); color:var(--warn); }
  .b-disarm { background:rgba(154,163,178,.13); color:var(--txt2); }
  .b-alive { background:rgba(56,189,248,.13); color:var(--claude); }
  .b-dead { background:rgba(248,113,113,.13); color:var(--bad); }
  .stale { color:var(--bad); font-size:12.5px; }
  .warn-t { color:var(--warn); font-size:12.5px; }

  .sec { display:flex; align-items:center; gap:12px; margin:30px 0 14px; }
  .sec .t { font-size:12px; font-weight:700; color:var(--txt2); letter-spacing:.1em;
            text-transform:uppercase; }
  .sec .ln { flex:1; height:1px; background:var(--border); }
  .sec .cnt { color:var(--txt3); font-size:11.5px; }

  table { width:100%; border-collapse:collapse; background:var(--panel);
          border:1px solid var(--border); border-radius:12px; overflow:hidden; }
  th,td { text-align:left; padding:9px 14px; font-size:13px; border-bottom:1px solid #1b202a; }
  th { color:var(--txt3); font-weight:600; font-size:11.5px; text-transform:uppercase; letter-spacing:.05em;
       background:var(--panel2); }
  tr:last-child td { border-bottom:none; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size:12px; }
  .dim { color:var(--txt3); }
  .zombie td { color:var(--txt3); }
  .ztag { color:var(--bad); font-size:11px; }

  .ev { display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px; font-weight:600; white-space:nowrap; }
  .ev-start { background:rgba(56,189,248,.13); color:var(--claude); }
  .ev-busy { background:rgba(74,222,128,.13); color:var(--ok); }
  .ev-idle { background:rgba(250,204,21,.11); color:var(--warn); }
  .ev-end { background:rgba(248,113,113,.13); color:var(--bad); }
  .ev-arm { background:rgba(192,132,252,.13); color:#c084fc; }
  .ev-other { background:rgba(154,163,178,.13); color:var(--txt2); }
  tr.fresh td { animation: flash 1.2s ease-out; }
  @keyframes flash { 0%{background:rgba(74,222,128,.18)} 100%{background:transparent} }

  button { font:inherit; font-weight:600; padding:8px 14px; border-radius:9px; border:1px solid var(--border2);
           background:var(--panel2); color:var(--txt); cursor:pointer; transition: all .12s; }
  button:hover { background:#232936; }
  button.primary { background:rgba(74,222,128,.16); border-color:transparent; color:var(--ok); }
  button.danger { background:rgba(248,113,113,.13); border-color:transparent; color:var(--bad); }
  button.accent { background:rgba(100,128,243,.15); border-color:rgba(100,128,243,.3); color:#9db1f8; }
  button.ghost { background:transparent; border-color:transparent; color:var(--txt2); opacity:.7; }
  .acard:hover button.ghost { opacity:1; }
  button.small { padding:4px 11px; font-size:12px; border-radius:7px; }

  .steps ol { margin:8px 0; padding-left:20px; }
  .steps li { margin:6px 0; font-size:13.5px; }
  code { background:rgba(255,255,255,.06); padding:2px 8px; border-radius:5px; font-size:12.5px;
         color:#7dd3fc; font-family: ui-monospace, Consolas, monospace; }
  dl.vocab dt { font-weight:650; font-size:13.5px; margin-top:16px; }
  dl.vocab dd { margin:3px 0 0; font-size:13px; color:var(--txt2); max-width:640px; }
  .cmds { display:grid; grid-template-columns: repeat(auto-fill,minmax(290px,1fr)); gap:8px; }
  .cmd { display:flex; align-items:center; gap:10px; border:1px solid var(--border);
         border-radius:10px; padding:8px 12px; }
  .cmd code { background:none; }
  .cmd .d { color:var(--txt2); font-size:12.5px; flex:1; }
  .foot { color:var(--txt3); font-size:11.5px; text-align:center; padding:12px 0 4px; }
</style>
</head>
<body>
  <aside class="side">
    <div class="brand">
      <div class="logo">
        <img src="/assets/arm_logo_64.png" onerror="this.style.display='none'">
        <div>
          <div class="t">ARM</div>
          <div class="s">Agent Runtime Manager</div>
        </div>
      </div>
    </div>
    <nav>
      <button class="nav-item on" data-v="overview"><span class="ic">⌂</span>概览</button>
      <button class="nav-item" data-v="sessions"><span class="ic">◱</span>会话</button>
      <button class="nav-item" data-v="events"><span class="ic">◷</span>事件</button>
      <button class="nav-item" data-v="lidtest"><span class="ic">◭</span>合盖实测</button>
      <button class="nav-item" data-v="help"><span class="ic">?</span>帮助</button>
    </nav>
    <div class="foot">
      <button id="guard-btn" class="guard-btn guard-off" onclick="toggleGuard()">开启保护</button>
      <div class="foot" style="margin-top:8px" >daemon 自启 · hooks 自愈</div>
    </div>
  </aside>

  <div class="main">
    <div class="topbar">
      <span class="title" id="vtitle">概览</span>
      <span id="hbadge" class="hbadge hb-ok"><span class="d"></span><span id="htext">加载中…</span></span>
      <span class="right"><span id="now"></span> · 每 2 秒刷新</span>
    </div>

    <div class="view">
      <!-- ===== 概览 ===== -->
      <section class="page on" id="p-overview">
        <h2 class="ph">Agent 会话</h2>
        <div class="pd">干活中的亮灯呼吸 · 会话窗口开着 ≠ 在跑任务（90 秒无活动视为本轮结束）· "开启保护"=布防，任务干活自动升级为保护</div>
        <div class="acards" id="hero"></div>

        <div class="sec"><span class="t">系统</span><span class="ln"></span>
          <button class="small primary" onclick="toggleGuardBtn(this)">开启保护</button>
        </div>
        <div class="kpis">
          <div class="kpi"><span class="k">保护状态</span><span class="v"><b id="prot" class="badge b-disarm">…</b></span><span class="n" id="prot-reason"></span><span class="stale" id="prot-stale" hidden>守护已停止，状态已失效</span></div>
          <div class="kpi"><span class="k">daemon 心跳</span><span class="v"><b id="hb" class="badge b-dead">…</b></span><span class="n" id="hb-note"></span></div>
          <div class="kpi"><span class="k">电源 · 待机</span><span class="v" id="power">…</span><span class="n" id="standby"></span><span class="warn-t" id="power-note"></span></div>
        </div>
        <div class="stale" id="hooks-warn" hidden>Claude hooks 缺失 → daemon 会自动修复；也可手动 arm init --yes</div>
      </section>

      <!-- ===== 会话 ===== -->
      <section class="page" id="p-sessions">
        <h2 class="ph">全部会话</h2>
        <div class="pd">活跃在前 · 僵尸置灰（daemon 自动回收 30 分钟无活动的僵尸）</div>
        <table id="agents"><thead><tr><th>状态</th><th>来源</th><th>会话</th><th>项目 / 目录</th><th>最后活动</th></tr></thead>
          <tbody></tbody></table>
      </section>

      <!-- ===== 事件 ===== -->
      <section class="page" id="p-events">
        <h2 class="ph">事件时间线</h2>
        <div class="pd">关键节点记录（任务开始/结束、保护动作、hooks 修复）· 最新在上 · hook 只在节点响，任务跑很久中间静默是正常的</div>
        <table id="events"><thead><tr><th style="width:105px">时间</th><th style="width:130px">事件</th><th>会话 / 项目</th></tr></thead>
          <tbody></tbody></table>
      </section>

      <!-- ===== 合盖实测 ===== -->
      <section class="page" id="p-lidtest">
        <h2 class="ph">合盖保活实测（核心验收）</h2>
        <div class="pd">验证"合盖+电池，任务照常跑"</div>
        <div class="steps">
          <ol>
            <li>给 Claude 派个任务，确认概览页亮灯"干活中"</li>
            <li>点 <b>① 记录基线</b>，然后拔电源、<b>合上盖子</b>去干别的</li>
            <li>回来点 <b>② 生成测试报告</b> —— 自动判定成败并给证据链</li>
          </ol>
          <div style="display:flex; gap:8px; margin:10px 0;">
            <button onclick="lidBaseline()">① 记录基线（合盖前）</button>
            <button class="primary" onclick="lidReport()">② 生成测试报告（开盖后）</button>
          </div>
          <div id="lid-report" hidden></div>
        </div>
      </section>

      <!-- ===== 帮助 ===== -->
      <section class="page" id="p-help">
        <h2 class="ph">ARM 是什么</h2>
        <div class="pd">让系统根据 AI Agent 的生命周期自动管理电源：任务在跑就保活（合盖+电池不停），结束就恢复省电。不是无脑防睡眠工具。</div>
        <div class="card">
          <b>它自动做的事</b>
          <ul style="margin:8px 0; padding-left:20px; font-size:13.5px;">
            <li>开机自启守护（daemon），无需手动开启</li>
            <li>Claude 干活 → 自动保护；结束 → 自动释放</li>
            <li>hooks 被其它工具冲掉 → 60 秒内自动修复</li>
          </ul>
          <b>支持的 Agent</b>
          <ul style="margin:8px 0; padding-left:20px; font-size:13.5px;">
            <li>✅ <b>Claude Code CLI</b> — 完整支持（hooks + 会话记录 + 进程三重感知）</li>
            <li>✅ <b>Claude 桌面端</b> — 本地会话与 CLI 同存储同管理，"继续会话"可直接恢复</li>
            <li>✅ <b>Codex CLI / Codex 桌面版</b> — 已支持（rollout 记录旁路 + 进程检测；任务开始/完成有显式标记，判定更精准）</li>
            <li>📋 规划：Kimi CLI / Gemini CLI / Aider / OpenCode</li>
            <li class="dim">边界：claude.ai 云端会话（ID 形如 session_）存于服务端，无法本地恢复</li>
          </ul>
        </div>
        <h2 class="ph" style="margin-top:18px;">概念词典</h2>
        <dl class="vocab">
          <dt>待机 S0（Modern Standby）</dt>
          <dd>Win11 的"假睡"：合盖后屏幕黑了，但系统会把前台进程降权甚至冻结——这就是"合盖任务卡住"的根源。arm 用电源保活对抗它。</dd>
          <dt>PROTECTING / ARMED / DISARMED</dt>
          <dd>保护中（有任务在守护）/ 待命（守护在线没任务）/ 未保护（守护关闭）。</dd>
          <dt>daemon 心跳</dt>
          <dd>守护进程每 2 秒报平安；35 秒没心跳视为已停止，状态自动判失效。</dd>
          <dt>Claude 进程（兜底探测）</dt>
          <dd>不依赖配置，直接看系统里有没有 Claude Code 进程——旧会话没 hooks 也能被保护。</dd>
          <dt>干活中（呼吸灯）</dt>
          <dd>这一轮对话正在工作（90 秒内有实际活动）。窗口开着没任务=不亮。</dd>
          <dt>事件时间线</dt>
          <dd>关键节点记录。hook 只在节点响，跑 30 分钟中间静默是正常的——看概览大卡。</dd>
          <dt>僵尸会话</dt>
          <dd>会话崩了没发退出信号。daemon 自动回收（30 分钟无活动）。</dd>
          <dt>transcript 旁路</dt>
          <dd>arm 直接读 Claude 会话记录文件判忙闲——hooks 丢了它也在。</dd>
          <dt>两个静默阈值（90 秒 / 120 秒）</dt>
          <dd>90 秒管"屏幕上显示忙不忙"（呼吸灯），120 秒管"什么时候判任务结束并释放保护"。显示先变闲、保护稍后松手，是设计不是卡了。</dd>
          <dt>电源键（ARM 运行期间）</dt>
          <dd>ARM 在托盘里时，电源键单击临时设为"不动作"——防止误按睡眠冻结 agent。长按 4 秒强关是硬件级、不受影响；开始菜单的"睡眠/关机"也不受影响。退出 ARM 后电源键恢复原样（退出=一切休息）。</dd>
        </dl>
      </section>
    </div>
    <div class="foot">ARM · 127.0.0.1 仅本机 · <span id="ver"></span></div>
  </div>

<script>
// 访问令牌（服务端注入；写 cookie 后同源 fetch 自动携带）
try { var _t = "__ARM_TOKEN__"; if (_t) document.cookie = "arm_t=" + _t + "; Path=/; SameSite=Strict"; } catch (e) {}
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const VT = {overview:'概览', sessions:'会话', events:'事件', lidtest:'合盖实测', help:'帮助'};

document.querySelectorAll('.nav-item').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.nav-item').forEach(x => x.classList.remove('on'));
    document.querySelectorAll('.page').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    $('p-' + b.dataset.v).classList.add('on');
    $('vtitle').textContent = VT[b.dataset.v] || b.dataset.v;
  };
});

function fmtTs(unix) {
  if (!unix) return '-';
  const d = new Date(unix * 1000);
  const now = new Date();
  const t = d.toLocaleTimeString('zh-CN', {hour12:false});
  const yest = new Date(now); yest.setDate(now.getDate()-1);
  if (d.toDateString() === now.toDateString()) return t;
  if (d.toDateString() === yest.toDateString()) return '昨天 ' + t;
  return (d.getMonth()+1) + '/' + d.getDate() + ' ' + t;
}
function relTs(unix) {
  const sec = Math.max(0, Math.round(Date.now()/1000 - unix));
  if (sec < 60) return sec + 's前';
  if (sec < 3600) return Math.floor(sec/60) + '分钟前';
  if (sec < 86400) return Math.floor(sec/3600) + '小时前';
  return Math.floor(sec/86400) + '天前';
}
function projName(sid, map) {
  const full = map && map[sid];
  if (!full) return '';
  const segs = full.split('\\').filter(Boolean);
  return segs[segs.length - 1] || full;
}
const EV_CLASS = { SessionStart:'ev-start', UserPromptSubmit:'ev-busy', Stop:'ev-idle', SessionEnd:'ev-end',
  PROTECT_START:'ev-arm', PROTECT_STOP:'ev-arm', PROTECT_ARM:'ev-arm', PROTECT_DISARM:'ev-arm', HOOKS_REPAIRED:'ev-arm' };
const EV_LABEL = { SessionStart:'会话开始', UserPromptSubmit:'任务开始', Stop:'一轮结束', SessionEnd:'会话退出',
  PROTECT_START:'🛡 开始保护', PROTECT_STOP:'释放保活', PROTECT_ARM:'进入待命', PROTECT_DISARM:'关闭保护', HOOKS_REPAIRED:'修复 hooks' };
function evBadge(ev) {
  return '<span class="ev ' + (EV_CLASS[ev]||'ev-other') + '">' + (EV_LABEL[ev]||ev) + '</span>';
}

let lastFreshTs = 0;
let sProj = {};
let lastSnap = null;

function renderHealth(s) {
  const problems = [];
  if (s.hooks && s.hooks.installed === false) problems.push('hooks 缺失（自动修复中）');
  const daemonDead = !(s.daemon && s.daemon.alive);
  const hasBusy = (s.live_sessions || []).length > 0;
  if (daemonDead) problems.push('daemon 未运行');
  const hb = $('hbadge');
  if (problems.length) {
    hb.className = 'hbadge ' + (hasBusy && daemonDead ? 'hb-bad' : 'hb-warn');
    $('htext').textContent = '⚠ ' + problems.join(' · ');
  } else {
    hb.className = 'hbadge hb-ok';
    $('htext').textContent = '正常 · 守护中' + (hasBusy ? ' · 任务运行中' : '');
  }
}
function renderHero(s) {
  const lv = s.live_sessions || [];
  const hero = $('hero');
  if (!lv.length) {
    hero.innerHTML = '<div class="acard off"><div class="band" style="background:var(--border)"></div>' +
      '<div class="inner"><div class="nm" style="font-weight:500;color:var(--txt2);font-size:14.5px">当前没有正在干活的会话</div>' +
      '<div class="tagline" style="color:var(--txt3)">给 Claude / Codex 派个任务，这里就会亮灯</div></div></div>';
    return;
  }
  hero.innerHTML = lv.slice(0, 6).map(x => {
    const nm = projName(x.session_id, sProj) || x.project || '会话';
    const isCodex = x.agent === 'codex';
    const origin = x.origin || 'cli';
    const tagCls = (isCodex ? 'codex' : 'claude') + '-' + (origin === 'desktop' ? 'desktop' : 'cli');
    const tagTxt = (isCodex ? 'Codex' : 'Claude') + (origin === 'desktop' ? ' 桌面端' : ' CLI');
    const act_sil = x.silence_s < 60 ? '刚刚活动' :
      (x.silence_s < 3600 ? Math.floor(x.silence_s/60) + ' 分钟前活动' : Math.floor(x.silence_s/3600) + ' 小时前活动');
    const sid = x.session_id || '';
    const resumeBtn = origin === 'desktop' ? '' :
      '<button class="small accent" data-act="resume" data-sid="' + esc(sid) + '" data-agent="' + x.agent + '">继续会话</button>';
    return '<div class="acard' + (isCodex ? ' codex' : '') + '">' +
      '<div class="band"></div>' +
      '<div class="inner">' +
      '<div class="nm">' + esc(nm) + '</div>' +
      '<div class="tagline"><span class="atag ' + tagCls + '">' + tagTxt + '</span><span>' + act_sil + '</span></div>' +
      '<div class="meta-row">' + resumeBtn +
        '<button class="small ghost" data-act="copycmd" data-sid="' + esc(sid) + '" data-agent="' + (x.agent||'claude') +
        '" title="复制恢复命令——粘贴到任意终端即可进入该会话">⧉ 复制命令</button></div>' +
      '</div></div>';
  }).join('');
}
document.getElementById('hero').addEventListener('click', ev => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const sid = btn.dataset.sid;
  if (btn.dataset.act === 'copycmd') copyResumeCmd(btn, sid, btn.dataset.agent);
  else if (btn.dataset.act === 'resume') resumeSession(btn, sid, btn.dataset.agent);
});
async function copyResumeCmd(btn, sid, agent) {
  try {
    const r = await fetch('/api/resume-cmd', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: sid, agent: agent})});
    const d = await r.json();
    await navigator.clipboard.writeText(d.cmd);
    btn.textContent = '✓ 已复制命令';
  } catch (e) {
    btn.textContent = '失败';
  }
  setTimeout(() => { btn.textContent = '⧉ 命令'; }, 1400);
}
async function resumeSession(btn, sid, agent) {
  btn.textContent = '定位中…';
  const r = await fetch('/api/resume', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id: sid, agent: agent || 'claude'})});
  const d = await r.json();
  const msgs = {focused: '已置前', 'already-running': '会话已在终端中', opened: '已在新终端打开', 'opened-desktop': '已在桌面端打开', 'opened-codex': '已在新终端打开 Codex', 'opened-codex-desktop': '桌面端已唤起 · 标题已复制，粘贴到搜索框定位'};
  btn.textContent = d.ok ? (msgs[d.action] || '完成') : '失败';
  setTimeout(() => { btn.textContent = '继续会话'; }, 1800);
}
function renderStrip(s) {
  const p = s.protection;
  $('prot').className = 'badge ' + (p.state==='PROTECTING'?'b-protect':p.state==='ARMED'?'b-armed':'b-disarm');
  $('prot').textContent = p.state;
  $('prot-reason').textContent = p.reason || '';
  $('prot-stale').hidden = !p.stale;
  $('hooks-warn').hidden = s.hooks.installed !== false;
  if (s.daemon && s.daemon.age_s != null) {
    $('hb').className = 'badge ' + (s.daemon.alive?'b-alive':'b-dead');
    $('hb').textContent = s.daemon.alive ? '存活' : '已停止';
    $('hb-note').textContent = s.daemon.alive ? Math.round(s.daemon.age_s) + ' 秒前有心跳' : '';
  } else { $('hb').className = 'badge b-dead'; $('hb').textContent = '从未'; }
  const pw = s.power;
  $('power').textContent = pw.ac_online===true ? 'AC 电源' : pw.ac_online===false ? ('电池 ' + (pw.battery_pct ?? '?') + '%') : '未知';
  $('power-note').textContent = (pw.ac_online===false && pw.low_battery) ? '电量低，建议接电' : '';
  $('standby').textContent = s.standby;
  // 侧栏守护按钮（daemon 死时变"启动守护"）
  const gb = $('guard-btn');
  const daemonDead = !(s.daemon && s.daemon.alive) || p.stale;
  const protecting = p.state === 'PROTECTING' && !daemonDead;
  if (daemonDead) {
    gb.className = 'guard-btn guard-off';
    gb.textContent = '启动守护';
    gb.dataset.mode = 'dead';
  } else {
    gb.className = 'guard-btn ' + (protecting ? 'guard-on' : 'guard-off');
    gb.textContent = protecting ? '释放保护' : '开启保护';
    gb.dataset.mode = protecting ? 'on' : 'off';
  }
}
function toggleGuard() {
  const m = $('guard-btn').dataset.mode;
  if (m === 'dead') { act('start-daemon'); return; }
  act(m === 'on' ? 'release' : 'protect');
}
function renderEvents(s) {
  const evs = s.events || [];
  const tb = $('events').tBodies[0];
  tb.innerHTML = evs.map((e, i) => {
    const fresh = (i === 0 && e.ts !== lastFreshTs && lastFreshTs !== 0);
    const pj = projName(e.session_id, sProj);
    return '<tr' + (fresh ? ' class="fresh"' : '') + '><td class="mono">' + fmtTs(e.ts) + '</td>' +
      '<td>' + evBadge(e.event) + '</td>' +
      '<td class="mono dim">' + esc((e.session_id||'-').slice(0,8)) +
      (pj ? ' <span style="color:#7dd3fc">' + esc(pj) + '</span>' : '') + '</td></tr>';
  }).join('') || '<tr><td colspan="3" class="dim">（暂无事件）</td></tr>';
  if (evs.length) lastFreshTs = evs[0].ts;
}
function renderAgents(s) {
  const liveSet = new Set((s.live_sessions||[]).map(x => x.session_id));
  const rest = (s.agents||[]).filter(a => !liveSet.has(a.session_id));
  // 排序（2026-09-28 合理化）：RUNNING 在前；其余终态（FINISHED/STOPPED）合为一组
  // 纯按时间倒序——终态之间不该再分尊卑（老的 FINISHED 压过新的 STOPPED 很怪）
  const rank = a => (a.state==='RUNNING' ? 0 : 1);
  rest.sort((a,b) => rank(a)-rank(b) || ((b.updated_ts||0)-(a.updated_ts||0)));
  $('agents').tBodies[0].innerHTML = rest.map(a => {
    const sub = a.substate ? '/' + a.substate : '';
    const z = a.zombie ? ' <span class="ztag">僵死</span>' : '';
    const nm = projName(a.session_id, sProj);
    const cOrigin = a.origin || (a.source === 'claude_hook' ? 'cli' : 'cli');
    const cCls = (a.source && a.source.indexOf('codex') >= 0 ? 'codex' : 'claude') + '-' + (cOrigin === 'desktop' ? 'desktop' : 'cli');
    const cTag = ' <span class="atag ' + cCls + '">' + (a.source && a.source.indexOf('codex') >= 0 ? 'Codex ' : 'Claude ') + (cOrigin === 'desktop' ? '桌面端' : 'CLI') + '</span>';
    return '<tr' + (a.zombie ? ' class="zombie"' : '') + '><td><b>' + a.state + esc(sub) + '</b>' + z + '</td>' +
      '<td class="dim">' + cTag + '</td><td class="mono">' + esc((a.session_id||'-').slice(0,8)) + '</td>' +
      '<td class="mono dim" title="' + esc(a.cwd||'') + '">' + esc(nm || (a.cwd||'-')) + '</td>' +
      '<td title="' + fmtTs(a.updated_ts) + '">' + relTs(a.updated_ts) + '</td></tr>';
  }).join('') || '<tr><td colspan="5" class="dim">（无）</td></tr>';
}
async function load() {
  try {
    const r = await fetch('/api/state');
    const s = await r.json();
    lastSnap = s;
    sProj = s.proj_by_sid || {};
    renderHealth(s);
    renderHero(s);
    renderStrip(s);
    renderEvents(s);
    renderAgents(s);
    $('now').textContent = new Date().toLocaleTimeString('zh-CN', {hour12:false});
  } catch (e) { $('htext').textContent = '加载失败：' + e; }
}
async function act(name) { await fetch('/api/' + name, {method:'POST'}); load(); }
async function lidBaseline() {
  await fetch('/api/lid-baseline', {method:'POST'});
  $('lid-report').hidden = false;
  $('lid-report').innerHTML = '<div class="steps">✅ 基线已记录。拔电源、<b>合上盖子</b>去干别的，回来点"② 生成测试报告"。</div>';
}
async function lidReport() {
  const r = await fetch('/api/lid-report');
  const rep = await r.json();
  const el = $('lid-report');
  el.hidden = false;
  if (rep.verdict === 'no_baseline') {
    el.innerHTML = '<div class="steps">还没有基线。合盖前先点 <b>① 记录基线</b>。</div>';
    return;
  }
  const pass = rep.verdict === 'pass';
  const rows = rep.evidence.map(e => {
    const mark = e.ok === true ? '✅' : e.ok === false ? '❌' : '⚠️';
    return '<tr><td>' + mark + ' ' + e.check + '</td><td class="mono dim">' + esc(e.detail) + '</td></tr>';
  }).join('');
  el.innerHTML = '<div class="steps"><div style="font-size:16px;font-weight:800;color:' + (pass?'var(--ok)':'var(--bad)') + ';margin-bottom:6px">' +
    (pass?'✅ 通过':'❌ 未通过') + ' — 合盖保活测试报告</div>' +
    '<div class="dim" style="font-size:12.5px;margin-bottom:6px">基线 ' + fmtTs(rep.baseline_ts) + '</div>' +
    '<table><tbody>' + rows + '</tbody></table></div>';
}
load();
setInterval(load, 2000);
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    store: Store  # 类属性，serve() 里注入
    token: Optional[str] = None  # 访问令牌（None=不校验，测试/兼容用）

    def log_message(self, *a):  # 静默默认访问日志
        pass

    def _authorized(self) -> bool:
        if not self.token:
            return True
        # 载体 1：query ?t=
        from urllib.parse import urlparse, parse_qs

        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [None])[0] == self.token:
            return True
        # 载体 2：Cookie arm_t=（首屏 Set-Cookie 后同源 fetch 自动携带）
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip() == f"arm_t={self.token}":
                return True
        return False

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        full_path = self.path                      # auth 需要 query（token 在 ?t=）
        self.path = full_path.split("?", 1)[0]     # 路由用纯路径
        # 静态资源豁免 token（图片无敏感数据；<img> 加载不带 query）
        if self.path.startswith("/assets/"):
            from pathlib import Path as _P

            asset = _P(__file__).parent / "assets" / _P(self.path[len("/assets/"):]).name
            if asset.exists() and asset.suffix in (".png", ".ico"):
                ctype = "image/png" if asset.suffix == ".png" else "image/x-icon"
                self._send(200, asset.read_bytes(), ctype)
            else:
                self._send(404, b"not found", "text/plain")
            return
        self.path = full_path                      # _authorized 自己解析 query
        if not self._authorized():
            self._send(403, b"forbidden", "text/plain")
            return
        self.path = self.path.split("?", 1)[0]     # 校验通过后再剥离，进入路由
        if self.path in ("/", "/index.html"):
            # 首屏：注入访问令牌（页面 JS 写 cookie，后续同源 fetch 自动携带）
            html = _HTML.replace("__ARM_TOKEN__", self.token or "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            if self.token:
                self.send_header("Set-Cookie", f"arm_t={self.token}; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/api/state":
            self._json(_collect_state(self.store))
        elif self.path == "/api/lid-report":
            self._json(_lid_test_report(self.store))
        elif self.path == "/api/events":
            rows = self.store.recent_agent_events(25)
            self._json({"events": [dict(r) for r in rows]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            self._send(403, b"forbidden", "text/plain")
            return
        self.path = self.path.split("?", 1)[0]
        if self.path == "/api/lid-baseline":
            # 生成基线快照文件（合盖前点击）
            from arm.core import paths as _paths

            f = _paths.data_dir() / "lid_test_baseline.txt"
            import time as _t

            f.write_text(
                f"# baseline_ts={_t.time()}\n"
                + f"# 生成于 {datetime_fmt(_t.time())} —— 合盖前基线\n"
                + json.dumps(_collect_state(self.store), ensure_ascii=False, indent=1),
                encoding="utf-8")
            self._json({"ok": True, "path": str(f)})
        elif self.path == "/api/protect":
            # 只写意图，daemon（唯一裁判）两拍内跟随——与托盘一致，避免双路径状态分叉
            self.store.set_protection("ARMED", reason="user arm (ui)")
            self._json({"ok": True, "action": "protect"})
        elif self.path == "/api/resume-cmd":
            # 兜底：返回可直接粘贴到终端的恢复命令（点击复制用）
            body = {}
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                pass
            sid = body.get("session_id") or ""
            agent = body.get("agent") or "claude"
            import re as _re

            if not _re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                self._json({"ok": False, "error": "bad session_id"}, 400)
                return
            cmd = (f"codex resume {sid}" if agent == "codex"
                   else f"claude --resume {sid}")
            self._json({"ok": True, "cmd": cmd})
        elif self.path == "/api/resume":
            # 在新终端窗口恢复指定 Claude 会话：claude --resume <session_id>
            body = {}
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                pass
            sid = body.get("session_id") or ""
            # 防注入：session_id 只允许 uuid 安全字符
            import re as _re

            if not _re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
                self._json({"ok": False, "error": "bad session_id"}, 400)
                return
            try:
                import os as _os
                import subprocess

                # ① 已有该会话的活进程（--resume/--session-id <id>）→ 唤其窗口到最前，不新开
                import psutil

                target_pid = None
                for proc in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        cl = " ".join(proc.info.get("cmdline") or [])
                        if "claude" not in cl.lower():
                            continue
                        if (f"--resume {sid}" in cl or f"--resume={sid}" in cl
                                or f"--session-id {sid}" in cl):
                            target_pid = proc.info["pid"]
                            break
                    except Exception:
                        continue
                # 两级窗口匹配（用户手动 claude 起的会话命令行无 id，命令行精确匹
                # 配只对"经 arm/带 --resume 恢复"的会话有效）：
                #   ① 精确：窗口属于该 pid，或标题含会话 id 前 8 位
                #   ② 兜底：存在标题含 "Claude Code" 的终端窗口（WT 会话标题特征）
                #      → 置前最近的那个（不新开终端，避免重复；用户肉眼确认）
                import win32con
                import win32gui
                import win32process  # GetWindowThreadProcessId 在这里（win32gui 没有）

                candidate = None  # (hwnd, 精确度)
                fallback = None

                def _cb(hwnd, _):
                    nonlocal candidate, fallback
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd) or ""
                    _, wpid = win32process.GetWindowThreadProcessId(hwnd)
                    if wpid == target_pid or sid[:8] in title:
                        candidate = hwnd
                        return False  # 精确命中，停止枚举
                    if "Claude Code" in title and fallback is None:
                        fallback = hwnd
                    return True

                if target_pid:
                    win32gui.EnumWindows(_cb, None)
                hwnd_to_focus = candidate or fallback
                if hwnd_to_focus:
                    win32gui.ShowWindow(hwnd_to_focus, win32con.SW_RESTORE)
                    try:
                        win32gui.SetForegroundWindow(hwnd_to_focus)
                    except Exception:
                        pass
                    action = "focused" if candidate else "focused-terminal"
                    self._json({"ok": True, "action": action})
                    return
                # 没有可置前的窗口（可能在 WT 标签里）→ 不新开，提示已存在
                if target_pid or fallback is not None:
                    self._json({"ok": True, "action": "already-running"})
                    return

                # ② 来源路由（从哪来回哪去）：桌面端会话→唤起桌面端；CLI→终端 resume
                # Codex 会话路由（从哪来回哪去）：
                #   桌面版创建（originator=codex_work_desktop / source_kind=chatgpt）→ 唤起桌面应用
                #   CLI 创建 → 终端 codex resume <id>
                codex_origin = None
                try:
                    from arm.sensors.codex_transcript import (
                        busy_codex_sessions, read_sqlite_catalog, scan_rollouts,
                    )

                    for r in read_sqlite_catalog():
                        if r["thread_id"] == sid:
                            codex_origin = ("desktop"
                                            if (r.get("source_kind") == "chatgpt"
                                                or r.get("originator") == "codex_work_desktop")
                                            else "cli")
                            break
                    if codex_origin is None:
                        for info in scan_rollouts(max_age_s=30 * 86400):
                            if info["session_id"] == sid:
                                codex_origin = ("desktop"
                                                if info.get("originator") == "codex_work_desktop"
                                                else "cli")
                                break
                except Exception:
                    codex_origin = None

                if codex_origin == "desktop":
                    # 桌面端会话：无"继续会话"（UI 上已不显示按钮）。
                    # 兜底提示：独立 APP，点开桌面端即见该会话。
                    self._json({"ok": True, "action": "use-desktop-app",
                                "hint": "桌面端会话：打开 Codex 桌面应用即见，无需跳转"})
                    return
                if codex_origin == "cli":
                    env2 = dict(_os.environ)
                    env2.pop("CLAUDE_CODE_CHILD_SESSION", None)
                    wt2 = subprocess.run(["where", "wt.exe"], capture_output=True, text=True)
                    if wt2.returncode == 0:
                        subprocess.Popen(
                            ["wt.exe", "cmd", "/k", "codex", "resume", sid],
                            creationflags=0x08000000, env=env2)
                    else:
                        subprocess.Popen(
                            ["cmd", "/c", "start", "cmd", "/k", "codex", "resume", sid],
                            creationflags=0x08000000, env=env2)
                    self._json({"ok": True, "action": "opened-codex"})
                    return
                # codex_origin 为 None → 不是 Codex 会话，继续走 Claude 路由

                from arm.sensors.transcript import session_origin

                origin = session_origin(sid)
                if origin == "desktop":
                    # 桌面端会话：claude:// 协议唤起桌面应用（会话列表里可见该会话）
                    subprocess.Popen(["cmd", "/c", "start", "", "claude:"],
                                     creationflags=0x08000000)
                    self._json({"ok": True, "action": "opened-desktop"})
                    return

                # CLI 会话：干净环境 + 终端 resume
                env = dict(_os.environ)
                env.pop("CLAUDE_CODE_CHILD_SESSION", None)
                env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"  # 官方变量名（强制保存 transcript）

                # 优先 Windows Terminal（体验好）；退回 conhost 新 cmd 窗口
                wt = subprocess.run(["where", "wt.exe"], capture_output=True, text=True)
                if wt.returncode == 0:
                    subprocess.Popen(
                        ["wt.exe", "cmd", "/k", "claude", "--resume", sid],
                        creationflags=0x08000000, env=env)
                else:
                    subprocess.Popen(
                        ["cmd", "/c", "start", "cmd", "/k", "claude", "--resume", sid],
                        creationflags=0x08000000, env=env)
                self._json({"ok": True, "action": "opened"})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
        elif self.path == "/api/start-daemon":
            import subprocess
            from pathlib import Path as _P

            arm_exe = _P.home() / ".local" / "bin" / "arm.exe"
            try:
                subprocess.Popen([str(arm_exe), "daemon"],
                                 creationflags=0x08000000,  # CREATE_NO_WINDOW
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                self._json({"ok": True, "action": "start-daemon"})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
        elif self.path == "/api/release":
            self.store.set_protection("DISARMED", reason="user release (ui)")
            self._json({"ok": True, "action": "release"})
        else:
            self._json({"error": "not found"}, 404)


def _lid_test_report(store: Store) -> dict:
    """合盖测试报告（L5 5.1）：对比基线快照与当前状态，自动判定 ✅/❌。

    基线文件：~/.arm/lid_test_baseline.txt 由"开保护合盖前"的手动/自动快照写入。
    判定证据链：
      1. daemon 心跳是否存活（保活进程没死）
      2. 事件流中保护期间是否有新事件（进程没冻结的证据）
      3. 基线后是否有 claude_hook 事件（任务在合盖期间推进/完成的痕迹）
    无基线时提示用户先生成基线。
    """
    import pathlib

    from arm.core import paths

    base_file = paths.data_dir() / "lid_test_baseline.txt"
    baseline_ts: Optional[float] = None
    if base_file.exists():
        # 基线文件第一行有时间戳注释：# baseline_ts=<unix>
        try:
            first = base_file.read_text(encoding="utf-8").splitlines()[0]
            if first.startswith("# baseline_ts="):
                baseline_ts = float(first.split("=", 1)[1])
        except Exception:
            baseline_ts = None

    now_evidence = []
    # 证据1：daemon 心跳
    hb = store.heartbeat_age()
    daemon_alive = hb is not None and hb < 35
    now_evidence.append({
        "check": "daemon 心跳",
        "ok": daemon_alive,
        "detail": f"{hb:.0f} 秒前" if hb is not None else "从未运行",
    })
    # 证据2：基线后的 claude_hook 事件（任务在合盖期间推进）
    import time as _t

    if baseline_ts is not None:
        rows = store.recent_agent_events(100)
        new_events = [dict(r) for r in reversed(rows)
                      if r["source"] == "claude_hook" and r["ts"] > baseline_ts]
        now_evidence.append({
            "check": "合盖后 hook 事件",
            "ok": len(new_events) > 0,
            "detail": f"{len(new_events)} 条新 hook 事件" +
                      (f"，最新 {datetime_fmt(new_events[-1]['ts'])}" if new_events else ""),
        })
        # 证据3：transcript 旁路（比 hooks 更可靠）
        try:
            from arm.sensors.transcript import active_sessions
            acts = active_sessions(idle_s=7200)
            now_evidence.append({
                "check": "transcript 旁路",
                "ok": len(acts) > 0,
                "detail": f"{len(acts)} 个会话近2小时有活动" +
                          (f"，最新 {acts[0]['project_path'][:40]} {acts[0]['age_s']:.0f}s前" if acts else ""),
            })
        except Exception as exc:
            now_evidence.append({"check": "transcript 旁路", "ok": None, "detail": f"不可用: {exc}"})

    verdict = None
    if baseline_ts is None:
        verdict = "no_baseline"
    else:
        verdict = "pass" if all(e["ok"] for e in now_evidence if e["ok"] is not None) else "fail"

    return {
        "baseline_ts": baseline_ts,
        "verdict": verdict,
        "evidence": now_evidence,
    }


def datetime_fmt(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _collect_state(store: Store) -> dict:
    p = power_sensor.snapshot()
    eff = store.get_effective_protection()
    sb = standby_sensor.supports_modern_standby()
    s3 = standby_sensor.supports_s3()
    hb_age = store.heartbeat_age()
    now = time.time()
    try:
        from arm.core import claude_hooks as ch
        hk = ch.hooks_installed()
    except Exception:
        hk = {"installed": None, "missing": []}
    try:
        from arm.adapters.claude import detect_claude_processes
        claude_procs = detect_claude_processes()
    except Exception:
        claude_procs = []
    # session -> 项目路径映射（transcript 旁路，显示用）
    try:
        from arm.sensors.transcript import scan_transcripts

        tinfos = scan_transcripts(max_age_s=7 * 86400)
        proj_by_sid = {t.session_id: t.project_path for t in tinfos}
    except Exception:
        tinfos = []
        proj_by_sid = {}
    # 细粒度忙闲（v2）：busy=这一轮真在干活；idle=会话在但本轮已结束
    try:
        from arm.sensors.transcript import busy_sessions

        all_busy = busy_sessions()
    except Exception:
        all_busy = []
    for x in all_busy:
        x["project"] = proj_by_sid.get(x["session_id"], x["project"])
        x["agent"] = "claude"
    # Codex 会话并入（T16）
    try:
        from arm.sensors.codex_transcript import busy_codex_sessions

        for x in busy_codex_sessions():
            all_busy.append({
                "session_id": x["session_id"],
                "project": x.get("title") or x.get("cwd") or "Codex 会话",
                "agent": "codex",
                "origin": x.get("origin") or "cli",
                "busy": x["busy"],
                "last_type": "task",
                "silence_s": x["silence_s"],
                "last_activity": x.get("last_activity"),
                "codex_title": x.get("title"),
                "source_kind": x.get("source_kind"),
            })
    except Exception:
        pass
    live = [x for x in all_busy if x["busy"]]
    agents = []
    for a in store.get_agent_states():
        d = dict(a)
        # 显示侧僵尸标注：RUNNING 但太久无事件（daemon 不在时没人回收）
        if d["state"] == "RUNNING":
            age = now - (d["updated_ts"] or now)
            if d["substate"] == "idle" and age > 120.0:
                d["zombie"] = True
            elif d["substate"] == "busy" and age > 1800.0:
                d["zombie"] = True
            else:
                d["zombie"] = False
        else:
            d["zombie"] = False
        agents.append(d)
    return {
        "now": now,
        "daemon": {"alive": hb_age is not None and hb_age < 35.0, "age_s": hb_age},
        "hooks": hk,
        "protection": {
            "state": eff["state"],
            "reason": eff["reason"],
            "stale": eff["stale"],
            "updated_ts": eff["updated_ts"],
        },
        "power": {
            "ac_online": p.ac_online,
            "battery_pct": p.battery_pct,
            "charging": p.charging,
            "low_battery": p.low_battery,
        },
        "standby": "S0 Modern Standby" if sb else ("S3" if s3 else "未知"),
        "agents": agents,
        "claude_procs": len(claude_procs),
        "proj_by_sid": proj_by_sid,
        "live_sessions": live,
        "recent_sessions": all_busy[:8],
        "events": [dict(e) for e in store.recent_agent_events(25)],
    }


def serve(host: str = "127.0.0.1", port: int = 8620,
          store: Optional[Store] = None, token: Optional[str] = None) -> None:
    """启动控制台（阻塞）。Ctrl+C 退出。

    token：访问令牌。提供时所有请求必须带 ?t=<token>（原生窗口 URL 自带），
    否则 403——浏览器仅凭端口无法访问（移除 Web 暴露面）。
    """
    store = store or Store()
    handler = type("H", (_Handler,), {"store": store, "token": token})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"ARM 控制台已启动: http://{host}:{port}  （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已退出。")
    finally:
        httpd.server_close()
