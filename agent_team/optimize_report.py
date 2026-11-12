"""流程优化 campaign 的 HTML 报告：读 result.json 确定性渲染，单文件、零外部依赖、深浅色可切换。

不调用 LLM：内容全部来自 collect 证据、优化者 optimize.json、评审 review-r*.json、门禁与优化日志。
所有外部文本一律 html.escape，防止证据里的尖括号/引号把页面结构弄坏。
"""
from __future__ import annotations

import html
import time
from typing import Any

from . import optimize as opt

STATUS_BADGE = {"merged": ("已合并", "ok"), "approved": ("评审通过", "ok"), "abandoned": ("已放弃", "bad"),
                "gate_failed": ("门禁未过", "bad"), "changes_requested": ("待修复", "warn"),
                "fixing": ("修复中", "warn"), "reviewing": ("评审中", "info"), "gated": ("门禁通过", "info"),
                "optimized": ("已优化", "info"), "optimizing": ("优化中", "info"), "selected": ("入选", "info"),
                "skipped": ("跳过", "mute")}
VERDICT_BADGE = {"improved": ("改善", "ok"), "unchanged": ("持平", "mute"), "regressed": ("退化", "bad"),
                 "no_data": ("数据不足", "mute")}
SEVERITY_BADGE = {"blocking": ("阻塞", "bad"), "major": ("中等", "warn"), "minor": ("建议", "mute")}
FINDING_STATUS = {"fixed": "已修复", "deferred": "遗留", "wontfix": "不修", "open": "未处理"}
CROSS_FLOW_BADGE = {"open": ("未处理", "warn"), "deferred": ("延后", "warn"), "done": ("已完成", "ok"),
                    "rejected": ("不做", "mute")}


def e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def badge(text: str, tone: str) -> str:
    return f'<span class="b {tone}">{e(text)}</span>'


def dur(s: float | None) -> str:
    if s is None:
        return "—"
    s = float(s)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.1f}h"


def ts(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t)) if t else "—"


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mute:#667085;--line:#e4e7ec;--accent:#3a5bd9;
--ok:#137a3d;--okbg:#e5f5ec;--bad:#b42318;--badbg:#fdecea;--warn:#93500b;--warnbg:#fdf2e1;
--info:#1f5bb8;--infobg:#e8f0fc;--mutebg:#eef0f3;--bar:#7f95e6;--bar2:#d0d7f5;--code:#f2f4f7}
[data-theme=dark]{--bg:#0f131a;--card:#171c25;--fg:#e6e9ef;--mute:#98a2b3;--line:#2a3240;--accent:#8aa2ff;
--ok:#6fd49a;--okbg:#14301f;--bad:#ff8a80;--badbg:#3a1916;--warn:#f5b964;--warnbg:#3a2a12;--info:#8ab4ff;
--infobg:#16263f;--mutebg:#222936;--bar:#6c84e0;--bar2:#2c3550;--code:#1e2430}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:0;min-width:0;overflow-wrap:anywhere}h4{font-size:13px;margin:16px 0 6px;color:var(--mute)}
.sub{color:var(--mute);font-size:13px}.top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 10px;cursor:pointer;flex:none}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi .v{font-size:22px;font-weight:600}.kpi .k{color:var(--mute);font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:12px 0}
.head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:500;white-space:nowrap}td{overflow-wrap:anywhere}.num{text-align:right;white-space:nowrap}
.scroll{overflow-x:auto}
.b{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;white-space:nowrap}
.ok{color:var(--ok);background:var(--okbg)}.bad{color:var(--bad);background:var(--badbg)}
.warn{color:var(--warn);background:var(--warnbg)}.info{color:var(--info);background:var(--infobg)}.mute{color:var(--mute);background:var(--mutebg)}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:12px;overflow-wrap:anywhere}
ul{margin:4px 0;padding-left:20px}li{margin:2px 0;overflow-wrap:anywhere}
.chg{border-left:3px solid var(--accent);padding:6px 0 6px 12px;margin:10px 0}
.ev{color:var(--mute);font-size:12px}details summary{cursor:pointer;color:var(--accent)}
.empty{color:var(--mute);font-style:italic}.tl td:first-child{white-space:nowrap;color:var(--mute)}
svg text{fill:var(--fg);font-size:11px}svg .m{fill:var(--mute)}
"""
JS = """const r=document.documentElement,k='opt-theme',s=localStorage.getItem(k);
if(s)r.dataset.theme=s;else if(matchMedia('(prefers-color-scheme: dark)').matches)r.dataset.theme='dark';
document.getElementById('tg').onclick=()=>{r.dataset.theme=r.dataset.theme==='dark'?'light':'dark';
localStorage.setItem(k,r.dataset.theme)};"""


# ---------------- 区块 ----------------

def overview(r: dict[str, Any]) -> str:
    t = r["totals"]
    w = r["window"]
    gate = r.get("main_gate") or {}
    gate_b = badge("主分支门禁通过", "ok") if gate.get("ok") else (
        badge("主分支门禁未过", "bad") if gate else badge("未合并", "mute"))
    kp = [("Job", t["jobs"]), ("流程", t["flows"]), ("入选优化", t["selected"]), ("已合并", t["merged"]),
          ("放弃", t["abandoned"]), ("健康/跳过", t["skipped"]), ("改动条目", t["changes"]),
          ("框架问题", t["framework_issues"]), ("未闭环跨流程请求", _open_cross_flow(r))]
    cards = "".join(f'<div class="kpi"><div class="v">{e(v)}</div><div class="k">{e(k)}</div></div>' for k, v in kp)
    return (f'<div class="top"><div><h1>流程优化报告 · {e(r["campaign_id"])}</h1>'
            f'<div class="sub">时间窗 {e(w["start"])} ~ {e(w["end"])}（{e(w["label"])}）｜基线 '
            f'<code>{e(r.get("base_commit"))}</code>（{e(r.get("base_branch"))}）｜合并后 '
            f'<code>{e(r.get("main_commit") or "—")}</code> {gate_b}｜生成于 {e(ts(r.get("finished_at")))}</div></div>'
            f'<button id="tg">深/浅色</button></div><div class="kpis">{cards}</div>')


def health_table(r: dict[str, Any]) -> str:
    rows = []
    for f in r["flows"]:
        m = f["metrics"]
        tot = m["totals"]
        label, tone = STATUS_BADGE.get(f["status"], (f["status"], "mute"))
        sig = "、".join(f"{r['signal_labels'].get(k, k)}×{v}" for k, v in (f.get("signal_counts") or {}).items())
        rows.append(f"<tr><td><b>{e(f['name'])}</b></td><td>{badge(label, tone)}</td>"
                    f"<td class=num>{m['jobs']}</td><td class=num>{pct(m['success_rate'])}</td>"
                    f"<td class=num>{dur(m['wall_p50_s'])}</td><td class=num>{dur(m['wall_max_s'])}</td>"
                    f"<td class=num>{tot['failures'] + tot['timeouts'] + tot['lifetime_kills']}</td>"
                    f"<td class=num>{tot['rejects']}</td><td class=num>{tot['retasks'] + tot['bypasses'] + tot['answers']}</td>"
                    f"<td class=num>{f['score']}</td><td>{e(sig) or '—'}</td><td>{e(f.get('reason'))}</td></tr>")
    return ('<h2>流程健康</h2><div class="card scroll"><table><tr><th>流程</th><th>状态</th><th>Job</th>'
            '<th>成功率</th><th>墙钟 p50</th><th>墙钟最大</th><th>失败/超时</th><th>打回</th><th>人工干预</th>'
            '<th>得分</th><th>问题信号</th><th>说明</th></tr>' + "".join(rows) + "</table></div>")


def span_chart(f: dict[str, Any]) -> str:
    """各步关键路径耗时 p50（横向条形，灰色是最大值），刻度取墙钟 p50 与最长步的较大者。"""
    tasks = [t for t in f["metrics"]["tasks"] if t["span_p50_s"]][:10]
    if not tasks:
        return ""
    wall = f["metrics"]["wall_p50_s"] or 0
    scale = max([wall] + [t["span_max_s"] or 0 for t in tasks]) or 1
    lw, bw, rh = 170, 560, 22
    h = rh * len(tasks) + 26
    parts = [f'<svg viewBox="0 0 {lw + bw + 90} {h}" width="100%" role="img" aria-label="各步耗时">']
    for i, t in enumerate(tasks):
        y = i * rh + 4
        mx = bw * (t["span_max_s"] or 0) / scale
        p50 = bw * t["span_p50_s"] / scale
        name = t["task_id"] if len(t["task_id"]) <= 24 else t["task_id"][:23] + "…"
        parts.append(f'<text x="{lw - 8}" y="{y + 13}" text-anchor="end">{e(name)}</text>'
                     f'<rect x="{lw}" y="{y + 3}" width="{mx:.1f}" height="13" rx="3" fill="var(--bar2)"/>'
                     f'<rect x="{lw}" y="{y + 3}" width="{p50:.1f}" height="13" rx="3" fill="var(--bar)"/>'
                     f'<text class="m" x="{lw + max(mx, p50) + 6:.1f}" y="{y + 13}">{e(dur(t["span_p50_s"]))}'
                     f' / {e(dur(t["span_max_s"]))}</text>')
    if wall:
        x = lw + bw * wall / scale
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="0" y2="{h - 18}" stroke="var(--bad)" stroke-dasharray="4 3"/>'
                     f'<text class="m" x="{x:.1f}" y="{h - 4}" text-anchor="middle">墙钟 p50 {e(dur(wall))}</text>')
    parts.append("</svg>")
    return "<h4>各步耗时 p50 / 最大（span：普通任务含重试之和，fan-out 取最慢一片）</h4>" + "".join(parts)


def _list(items: list[Any]) -> str:
    items = [x for x in items if x not in (None, "")]
    if not items:
        return '<span class="empty">无</span>'
    return "<ul>" + "".join(f"<li>{e(x if isinstance(x, str) else _plain(x))}</li>" for x in items) + "</ul>"


def _plain(x: Any) -> str:
    if isinstance(x, dict):
        return "；".join(f"{k}: {v}" for k, v in x.items() if v not in (None, "", []))
    return str(x)


def change_block(ch: dict[str, Any]) -> str:
    metric = ch.get("metric") or "—"
    base = ch.get("baseline")
    files = "".join(f"<code>{e(p)}</code> " for p in ch.get("files") or [])
    return (f'<div class="chg"><div class="head"><b>{e(ch.get("title"))}</b>'
            f'{badge(ch.get("direction_label") or ch.get("direction") or "其他", "info")}</div>'
            f'<div><b>问题：</b>{e(ch.get("problem"))}</div><div><b>改法：</b>{e(ch.get("change"))}</div>'
            f'<div><b>预期：</b>{e(ch.get("expected"))}｜<b>验证指标：</b><code>{e(metric)}</code> '
            f'基线 {e("—" if base is None else base)}</div><div class="ev">文件：{files or "—"}</div>'
            f'<details><summary>证据（{len(ch.get("evidence") or [])}）</summary>{_list(ch.get("evidence") or [])}'
            f'</details></div>')


def flow_card(f: dict[str, Any]) -> str:
    label, tone = STATUS_BADGE.get(f["status"], (f["status"], "mute"))
    ds = f.get("diffstat") or {}
    commits = " ".join(f"<code>{e(c)}</code>" for c in f.get("commits") or []) or "—"
    meta = (f'<div class="sub">plan <code>{e(f.get("plan"))}</code>｜Job {len(f["jobs"])} 个｜评审 '
            f'{f.get("review_rounds") or 0} 轮｜修复 {f.get("fix_rounds") or 0} 轮｜diff '
            f'+{ds.get("insertions", 0)}/−{ds.get("deletions", 0)}（{len(ds.get("files", []))} 文件）｜commit {commits}'
            + (f'｜合并 <code>{e(f.get("merge_commit"))}</code>' if f.get("merge_commit") else "") + "</div>")
    shared = [f"{a} 也被 {', '.join(o)} 使用" for a, o in (f.get("shared_agents") or {}).items()]
    gates = "".join(badge(f"门禁 r{g['round']} 通过" if g["ok"] else f"门禁 r{g['round']}：" + "、".join(g["failed"]),
                          "ok" if g["ok"] else "bad") + " " for g in f.get("gates") or [])
    reviews = "".join(badge(f"评审 r{rv['round']} {rv['verdict']}", "ok" if rv["verdict"] == "PASS" else "warn") + " "
                      for rv in f.get("reviews") or [])
    body = [f'<div class="card"><div class="head"><h3>{e(f["name"])}</h3>{badge(label, tone)}{gates}{reviews}</div>',
            meta]
    if f.get("summary"):
        body.append(f"<p>{e(f['summary'])}</p>")
    if f.get("reason") and f["status"] in ("abandoned", "skipped"):
        body.append(f"<p><b>原因：</b>{e(f['reason'])}</p>")
    body.append(span_chart(f))
    if f.get("changes"):
        body.append("<h4>改动</h4>" + "".join(change_block(ch) for ch in f["changes"]))
    if shared or f.get("requests_agents"):
        req = [f"{a} 归 {o}" for a, o in (f.get("requests_agents") or {}).items()]
        body.append("<h4>共享 agent</h4>" + _list(shared + req))
    for title, key in (("未做的建议", "not_done"), ("风险", "risks")):
        if f.get(key):
            body.append(f"<h4>{title}</h4>" + _list(f[key]))
    if f.get("cross_flow"):
        body.append(f'<p class="sub">跨流程请求 {len(f["cross_flow"])} 条，见「跨流程请求」小节</p>')
    body.append("</div>")
    return "".join(body)


def cross_flow_state(r: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """各流程的 cross_flow（旧字符串项现场归一化）与本 campaign 的闭环判定，口径同 pending_cross_flow。"""
    rows, flows = [], {}
    for f in r["flows"]:
        items = opt.normalize_cross_flow(f.get("cross_flow"), f["name"], f.get("requests_agents") or {})
        flows[f["name"]] = {"status": f.get("status"), "cross_flow": items}
        rows += [{**x, "_merged": f.get("status") == "merged"} for x in items]
    open_, closed = opt.cross_flow_ledger([{"campaign_id": r.get("campaign_id"), "flows": flows}])
    return rows, open_, closed


def _open_cross_flow(r: dict[str, Any]) -> int:
    return len(cross_flow_state(r)[1])


def _carried_cross_flow(r: dict[str, Any], rows: list[dict[str, Any]], closed: dict[str, str]) -> str:
    """往期遗留：collect 时 campaign.json 记下的 pending_cross_flow，标出本次是否已闭环。"""
    items = [x for x in r.get("pending_cross_flow") or [] if isinstance(x, dict) and x.get("id")]
    if not items:
        return ""
    merged = {f["name"] for f in r["flows"] if f.get("status") == "merged"}
    mine = {x["id"] for x in rows if x["_merged"]}
    trs = []
    for x in items:
        if x["id"] in closed:
            track = badge(f"本次已闭环（{closed[x['id']]}）", "ok")
        elif x.get("requester") in merged and x["id"] not in mine:
            track = badge("发起流程本次未再提，视为不再需要", "mute")
        else:
            track = badge("仍未闭环，下次提示", "warn")
        trs.append(f"<tr><td>{e(x.get('campaign_id'))}</td><td>{e(x.get('requester'))}</td>"
                   f"<td>{e(x.get('owner') or '—')}</td><td>{e(x.get('request'))}<div class=ev>{e(x['id'])}</div></td>"
                   f"<td>{track}</td></tr>")
    return ('<h4>往期遗留（collect 时）</h4><p class="sub">不计入上面的本次未闭环数。</p>'
            '<div class="card scroll"><table><tr><th>提出于</th><th>发起流程</th><th>owner</th><th>请求</th>'
            '<th>本次结果</th></tr>' + "".join(trs) + "</table></div>")


def cross_flow_section(r: dict[str, Any]) -> str:
    rows, open_, closed = cross_flow_state(r)
    carried = _carried_cross_flow(r, rows, closed)
    if not rows:
        return '<h2>跨流程请求</h2><div class="card empty">本次没有跨流程请求</div>' + carried
    untracked = sum(not x["_merged"] for x in rows)
    head = (f'<p class="sub">按 id 去重：未闭环 {len(open_)} 条（下次 collect 的 <code>pending_cross_flow</code> '
            f'会提示 owner 跟进），本次已闭环 {len(closed)} 条；发起流程未合并、不跟踪 {untracked} 条。'
            '只有已合并流程的请求与 done/rejected 才计入闭环判定。</p>')
    trs = []
    for x in rows:
        lb, tone = CROSS_FLOW_BADGE.get(x["status"], (x["status"], "mute"))
        if not x["_merged"]:
            track = badge("未合并，不跟踪", "mute")
        elif x["id"] in open_:
            track = badge("未闭环，下次提示", "warn")
        else:
            track = badge(f"已闭环（{closed.get(x['id'], '—')}）", "ok")
        evs = x["evidence"]
        ev = f'<details><summary>证据（{len(evs)}）</summary>{_list(evs)}</details>' if evs else ""
        trs.append(f"<tr><td>{e(x['requester'])}</td><td>{e(x['owner'] or '—')}</td>"
                   f"<td>{e(x['target_flow'] or '—')}</td><td>{e(x['request'])}"
                   f"<div class=ev>{e(x['id'])}</div>{ev}</td><td>{badge(lb, tone)}</td><td>{track}</td>"
                   f"<td>{e(x['verification'] or '—')}</td></tr>")
    return ('<h2>跨流程请求</h2>' + head + '<div class="card scroll"><table><tr><th>发起流程</th><th>owner</th>'
            '<th>目标流程</th><th>请求</th><th>状态</th><th>跟踪</th><th>验证口径</th></tr>'
            + "".join(trs) + "</table></div>" + carried)


def findings_table(r: dict[str, Any]) -> str:
    rows = []
    for f in r["flows"]:
        for it in f.get("findings") or []:
            sl, st = SEVERITY_BADGE.get(it.get("severity"), (it.get("severity") or "—", "mute"))
            status = it.get("status") or "open"
            rnd = f"r{it['round']}" + (f"→r{it['last_round']}" if it.get("last_round", it["round"]) != it["round"] else "")
            rows.append(f"<tr><td>{e(f['name'])}</td><td class=num>{rnd}</td><td>{badge(sl, st)}</td>"
                        f"<td>{e(it.get('title'))}<div class=ev>{e(it.get('location'))}</div></td>"
                        f"<td>{badge(FINDING_STATUS.get(status, status), 'ok' if status == 'fixed' else 'mute')}</td>"
                        f"<td>{e(it.get('resolution'))}</td></tr>")
    if not rows:
        return '<h2>评审台账</h2><div class="card empty">没有评审发现</div>'
    return ('<h2>评审台账</h2><div class="card scroll"><table><tr><th>流程</th><th>轮次</th><th>严重度</th>'
            '<th>问题</th><th>处理</th><th>说明</th></tr>' + "".join(rows) + "</table></div>")


def framework_section(r: dict[str, Any]) -> str:
    issues = r.get("framework_issues") or []
    cands = r.get("framework_candidates") or []
    out = ['<h2>框架问题</h2><p class="sub">不属于单条流程、需要改 <code>agent_team/</code> 等框架层的问题，'
           '已登记进 <code>coordinator/insights/backlog.json</code>，用 team-evolve 处理。</p>']
    if issues:
        for it in issues:
            rec = f" ×{it['occurrences']}" if (it.get("occurrences") or 1) > 1 else ""
            score = badge(f"得分 {it.get('score')}{rec}", "mute")
            out.append(f'<div class="card"><div class="head"><h3>{e(it["title"])}</h3>'
                       f'{badge(it["id"], "info")}{score}'
                       f'{badge(it.get("status", "open"), "warn" if it.get("status") == "open" else "ok")}</div>'
                       f'<div class="sub">目标：{e(it.get("target"))}｜影响 {e(it.get("impact"))}｜改动量 '
                       f'{e(it.get("effort"))}</div><p><b>问题：</b>{e(it.get("problem"))}</p>'
                       f'<p><b>建议：</b>{e(it.get("proposal"))}</p><details><summary>证据</summary>'
                       f'{_list(it.get("evidence") or [])}</details></div>')
    else:
        out.append('<div class="card empty">本次没有登记框架问题</div>')
    if cands:
        rows = "".join(f"<tr><td>{e(c['label'])}</td><td class=num>{c['count']}</td><td>{e('、'.join(c['flows']))}</td>"
                       f"<td>{_list(c['evidence'][:3])}</td></tr>" for c in cands)
        out.append('<h4>collect 自动识别的框架信号</h4><div class="card scroll"><table><tr><th>信号</th><th>次数</th>'
                   '<th>流程</th><th>证据（前 3 条）</th></tr>' + rows + "</table></div>")
    return "".join(out)


def effect_section(r: dict[str, Any]) -> str:
    eff = r.get("previous_effect") or []
    if not eff:
        return '<h2>上次优化效果</h2><div class="card empty">没有可对比的历史优化（首次运行，或上次改动后还没有新的 Job）</div>'
    rows = []
    for x in eff:
        lb, tone = VERDICT_BADGE.get(x["verdict"], (x["verdict"], "mute"))
        rows.append(f"<tr><td>{e(x['campaign_id'])}</td><td>{e(x['flow'])}</td><td>{e(x['title'])}</td>"
                    f"<td><code>{e(x['metric'] or '—')}</code></td><td class=num>{e('—' if x['baseline'] is None else x['baseline'])}</td>"
                    f"<td class=num>{e('—' if x['current'] is None else x['current'])}</td><td class=num>{x['jobs_after']}</td>"
                    f"<td>{badge(lb, tone)}</td></tr>")
    return ('<h2>上次优化效果</h2><div class="card scroll"><table><tr><th>campaign</th><th>流程</th><th>改动</th>'
            '<th>指标</th><th>基线</th><th>现在</th><th>改动后 Job</th><th>结论</th></tr>' + "".join(rows) + "</table></div>")


_EVENT_LABEL = {"campaign_started": "开始", "evidence_collected": "收集证据", "flow_selected": "入选",
                "flow_skipped": "跳过", "worktree_created": "建 worktree", "optimizer_done": "优化交付",
                "fix_done": "修复交付", "gate_result": "门禁", "review_started": "开始评审",
                "review_verdict": "评审结论", "fix_round": "进入修复", "merged": "合并", "merge_conflict": "合并冲突",
                "main_gate": "主分支门禁", "abandoned": "放弃", "framework_issue_recorded": "登记框架问题",
                "scope_extended": "扩展边界", "campaign_finished": "完成", "note": "备注"}
_SKIP_FIELDS = {"ts", "event", "flow"}


def timeline(r: dict[str, Any]) -> str:
    rows = []
    for ev in r.get("timeline") or []:
        detail = "；".join(f"{k}={v}" for k, v in ev.items() if k not in _SKIP_FIELDS and v not in (None, "", []))
        rows.append(f"<tr><td>{e(ts(ev.get('ts')))}</td><td>{e(_EVENT_LABEL.get(ev['event'], ev['event']))}</td>"
                    f"<td>{e(ev.get('flow', ''))}</td><td class=ev>{e(detail[:400])}</td></tr>")
    return ('<h2>时间线</h2><details class="card"><summary>优化日志（' + str(len(rows)) + ' 条）</summary>'
            '<div class="scroll tl"><table><tr><th>时间</th><th>事件</th><th>流程</th><th>详情</th></tr>'
            + "".join(rows) + "</table></div></details>")


def appendix(r: dict[str, Any]) -> str:
    gate = r.get("main_gate") or {}
    items = [f"主分支门禁：{'通过' if gate.get('ok') else ('未通过' if gate else '未执行')}"
             + (f"（{gate.get('file')}）" if gate.get("file") else "")]
    for f in r["flows"]:
        if f.get("commits"):
            items.append(f"{f['name']}：分支 {f.get('branch') or '—'}，commit {' '.join(f['commits'])}"
                         + (f"，合并 {f['merge_commit']}" if f.get("merge_commit") else ""))
    return '<h2>附录</h2><div class="card">' + _list(items) + "</div>"


def render(r: dict[str, Any]) -> str:
    active = [f for f in r["flows"] if f["status"] != "skipped"]
    skipped = [f for f in r["flows"] if f["status"] == "skipped"]
    detail = "".join(flow_card(f) for f in active) or '<div class="card empty">本次没有入选优化的流程</div>'
    if skipped:
        detail += "<h4>跳过的流程</h4>" + _list([f"{f['name']}：{f.get('reason')}" for f in skipped])
    body = (overview(r) + health_table(r) + "<h2>流程详情</h2>" + detail + findings_table(r)
            + cross_flow_section(r) + framework_section(r) + effect_section(r) + timeline(r) + appendix(r))
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>流程优化报告 {e(r["campaign_id"])}</title><style>{CSS}</style></head>'
            f'<body><main>{body}</main><script>{JS}</script></body></html>\n')
