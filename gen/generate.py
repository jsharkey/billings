#!/usr/bin/env python3
"""Generate SCHEDULE.md and docs/index.html from schedule.yaml.

Usage:
    python3 gen/generate.py          # regenerate outputs
    python3 gen/generate.py --check  # exit 1 if outputs are stale
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "schedule.yaml"
OUT_MD = ROOT / "SCHEDULE.md"
OUT_HTML = ROOT / "docs" / "index.html"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_START_HOUR = 8      # a work day is drawn as 08:00-16:00
DAY_HOURS = 8
MAX_DAYS = 365 * 6      # simulation guard
EPS = 1e-9


class ScheduleError(Exception):
    pass


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
@dataclass
class Item:
    area: str
    template: str
    item: str
    effort: float          # total effort for this item in this area
    progress: float        # 0..1


@dataclass
class Task:
    id: str
    name: str
    section: str
    kind: str              # work | lag | inspection | milestone
    after: list[str]
    crew: str | None = None
    template: str | None = None
    areas: list[str] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    effort: float = 0.0    # total effort
    progress: float = 0.0  # 0..1 overall
    lag: int = 0
    rule: str | None = None
    # results
    remaining: float = 0.0
    start: tuple[dt.date, float] | None = None   # (day, fraction into day)
    finish: tuple[dt.date, float] | None = None
    last: tuple[dt.date, float] | None = None    # last moment worked
    driver: str | None = None
    critical: bool = False

    @property
    def done(self) -> bool:
        return self.kind == "work" and self.remaining <= EPS


def parse_state(state) -> float:
    if state is None or state == "todo":
        return 0.0
    if state == "done":
        return 1.0
    if state == "na":
        return -1.0
    if isinstance(state, (int, float)) and 0 <= state <= 1:
        return float(state)
    raise ScheduleError(f"bad status value {state!r} (use done/todo/na or 0..1)")


def load(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def build_tasks(cfg: dict) -> dict[str, Task]:
    areas = cfg["areas"]
    templates = cfg["templates"]
    status = cfg.get("status") or {}
    crews = cfg["crews"]

    for a in status:
        if a not in areas:
            raise ScheduleError(f"status: unknown area {a!r}")
        for t, st in (status[a] or {}).items():
            if t not in templates:
                raise ScheduleError(f"status.{a}: unknown template {t!r}")
            if isinstance(st, dict):
                for i in st:
                    if i not in templates[t]["items"]:
                        raise ScheduleError(f"status.{a}.{t}: unknown item {i!r}")

    tasks: dict[str, Task] = {}
    for raw in cfg["tasks"]:
        tid = raw["id"]
        if tid in tasks:
            raise ScheduleError(f"duplicate task id {tid!r}")
        after = list(raw.get("after") or [])
        common = dict(id=tid, name=raw["name"], section=raw.get("section", "Tasks"), after=after)
        if raw.get("milestone"):
            t = Task(kind="milestone", **common)
        elif "inspection" in raw:
            if raw["inspection"] not in cfg["inspections"]:
                raise ScheduleError(f"{tid}: unknown inspection rule {raw['inspection']!r}")
            t = Task(kind="inspection", rule=raw["inspection"], **common)
        elif "lag" in raw:
            t = Task(kind="lag", lag=int(raw["lag"]), **common)
        else:
            crew = raw.get("crew")
            if crew not in crews:
                raise ScheduleError(f"{tid}: unknown crew {crew!r}")
            t = Task(kind="work", crew=crew, **common)
            if "template" in raw:
                tname = raw["template"]
                if tname not in templates:
                    raise ScheduleError(f"{tid}: unknown template {tname!r}")
                tpl = templates[tname]
                t.template = tname
                t.areas = list(raw["areas"])
                for a in t.areas:
                    if a not in areas:
                        raise ScheduleError(f"{tid}: unknown area {a!r}")
                    area = areas[a]
                    scale = float(area.get("scale", 1.0)) if tpl.get("scaled") else 1.0
                    atags = set(area.get("tags") or [])
                    st = (status.get(a) or {}).get(tname)
                    for iname, spec in tpl["items"].items():
                        if isinstance(spec, dict):
                            eff = float(spec["effort"])
                            itags = set(spec.get("tags") or [])
                            if itags and not (itags & atags):
                                continue
                        else:
                            eff = float(spec)
                        prog = parse_state(st.get(iname) if isinstance(st, dict) else st)
                        if prog < 0:   # na
                            continue
                        t.items.append(Item(a, tname, iname, eff * scale, prog))
                t.effort = sum(i.effort for i in t.items)
                t.progress = (sum(i.effort * i.progress for i in t.items) / t.effort) if t.effort else 1.0
            else:
                t.effort = float(raw["effort"])
                t.progress = parse_state(raw.get("progress"))
            t.remaining = t.effort * (1 - t.progress)
        tasks[tid] = t

    for t in tasks.values():
        for d in t.after:
            if d not in tasks:
                raise ScheduleError(f"{t.id}: unknown dependency {d!r}")
    topo_order(tasks)  # raises on cycles
    return tasks


def topo_order(tasks: dict[str, Task]) -> list[str]:
    order, state = [], {}

    def visit(tid, stack):
        if state.get(tid) == 2:
            return
        if state.get(tid) == 1:
            raise ScheduleError("dependency cycle: " + " -> ".join(stack + [tid]))
        state[tid] = 1
        for d in tasks[tid].after:
            visit(d, stack + [tid])
        state[tid] = 2
        order.append(tid)

    for tid in tasks:
        visit(tid, [])
    return order


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------
class Calendar:
    def __init__(self, cfg: dict):
        cal = cfg.get("calendar") or {}
        self.holidays = {to_date(d) for d in cal.get("holidays") or []}
        self.extra = {c: {to_date(d): float(v) for d, v in (m or {}).items()}
                      for c, m in (cal.get("extra_days") or {}).items()}
        self.skip = {c: {to_date(d) for d in (l or [])}
                     for c, l in (cal.get("skip_days") or {}).items()}
        self.crews = cfg["crews"]

    def capacity(self, crew: str, day: dt.date) -> float:
        if day in self.extra.get(crew, {}):
            return self.extra[crew][day]
        if day in self.skip.get(crew, set()):
            return 0.0
        spec = self.crews[crew]
        days = spec.get("days", WEEKDAYS[:5])
        if WEEKDAYS[day.weekday()] not in days:
            return 0.0
        # weekday crews observe holidays; weekend-pattern crews don't
        if set(days) <= set(WEEKDAYS[:5]) and day in self.holidays:
            return 0.0
        return float(spec.get("capacity", 1.0))

    def business_day(self, day: dt.date) -> bool:
        return day.weekday() < 5 and day not in self.holidays


def to_date(v) -> dt.date:
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def schedule(cfg: dict, tasks: dict[str, Task]) -> None:
    cal = Calendar(cfg)
    as_of = to_date(cfg["project"]["as_of"])
    rules = cfg["inspections"]
    order = topo_order(tasks)
    succ: dict[str, list[str]] = {t: [] for t in tasks}
    for t in tasks.values():
        for d in t.after:
            succ[d].append(t.id)

    # Priority: longest remaining path to the end, in rough calendar days.
    crew_rate = {}
    for c, spec in cfg["crews"].items():
        n = len(spec.get("days", WEEKDAYS[:5]))
        crew_rate[c] = n * float(spec.get("capacity", 1.0)) / 7.0
    tail: dict[str, float] = {}
    for tid in reversed(order):
        t = tasks[tid]
        own = {"work": (t.remaining / crew_rate[t.crew]) if t.crew else 0,
               "lag": t.lag, "inspection": 3, "milestone": 0}[t.kind]
        tail[tid] = own + max((tail[s] for s in succ[tid]), default=0)
    rank = {tid: i for i, tid in enumerate(tasks)}

    # Work already complete as of the status date.
    for t in tasks.values():
        if t.done:
            t.finish = (as_of, 1.0)

    def deps_finished(t: Task) -> bool:
        return all(tasks[d].finish is not None for d in t.after)

    def latest_dep(t: Task):
        best = None
        for d in t.after:
            f = tasks[d].finish
            if best is None or f > tasks[best].finish:
                best = d
        return best

    def resolve_events(today: dt.date) -> bool:
        changed = False
        for tid in order:
            t = tasks[tid]
            if t.kind == "work" or t.finish is not None or not deps_finished(t):
                continue
            ld = latest_dep(t)
            last = tasks[ld].finish[0] if ld else as_of
            t.driver = ld
            if t.kind == "milestone":
                t.start = t.finish = tasks[ld].finish if ld else (as_of, 1.0)
                changed = True
                continue
            elif t.kind == "lag":
                day = last + dt.timedelta(days=t.lag)
                t.start = (last, 1.0)
                t.finish = (day, 1.0)
                changed = True
                continue
            else:  # inspection
                rule = rules[t.rule]
                allowed = rule.get("days", WEEKDAYS[:4])
                earliest = last + dt.timedelta(days=int(rule.get("lead", 0)))
                req = last + dt.timedelta(days=1)       # request next business day
                while not cal.business_day(req):
                    req += dt.timedelta(days=1)
                day = req + dt.timedelta(days=1)        # inspected the day after
                while (WEEKDAYS[day.weekday()] not in allowed or day in cal.holidays
                       or day < earliest):
                    day += dt.timedelta(days=1)
            t.start = (day, 0.5)
            t.finish = (day, 0.5)
            changed = True
        return changed

    def ready(t: Task, day: dt.date, cursor: float) -> bool:
        for d in t.after:
            dep = tasks[d]
            if dep.finish is None:
                return False
            fday, ffrac = dep.finish
            if dep.kind == "work" and dep.crew == t.crew:
                if (fday, ffrac) > (day, cursor + EPS):
                    return False
            elif fday >= day:   # hand-offs take effect the next day
                return False
        return True

    current: dict[str, str | None] = {c: None for c in cfg["crews"]}
    last_worked: dict[str, str | None] = {c: None for c in cfg["crews"]}
    day = as_of + dt.timedelta(days=1)
    resolve_events(day)
    for _ in range(MAX_DAYS):
        if all(t.finish is not None for t in tasks.values()):
            break
        for crew in cfg["crews"]:
            cap = cal.capacity(crew, day)
            cursor = 0.0
            while cap - cursor > EPS:
                cur = current[crew]
                if cur is None or tasks[cur].finish is not None or not ready(tasks[cur], day, cursor):
                    cands = [t for t in tasks.values()
                             if t.kind == "work" and t.crew == crew and t.finish is None
                             and ready(t, day, cursor)]
                    if not cands:
                        break
                    cands.sort(key=lambda t: (-tail[t.id], rank[t.id]))
                    cur = cands[0].id
                    current[crew] = cur
                t = tasks[cur]
                if t.start is None:
                    t.start = (day, cursor / cap)
                    # Driver: whatever held this task back - its latest
                    # dependency, or the crew being busy on its previous task.
                    t.driver = latest_dep(t)
                    prev = last_worked[crew]
                    if prev and prev != t.id and (t.driver is None or
                                                  tasks[prev].last > tasks[t.driver].finish):
                        t.driver = prev
                use = min(cap - cursor, t.remaining)
                t.remaining -= use
                cursor += use
                t.last = (day, cursor / cap)
                last_worked[crew] = t.id
                if t.remaining <= EPS:
                    t.remaining = 0.0
                    t.finish = (day, cursor / cap)
                    current[crew] = None
        resolve_events(day)
        day += dt.timedelta(days=1)
    else:
        stuck = [t.id for t in tasks.values() if t.finish is None]
        raise ScheduleError(f"could not schedule (check crews/calendar): {stuck}")

    # Restore remaining effort for reporting.
    for t in tasks.values():
        if t.kind == "work":
            t.remaining = t.effort * (1 - t.progress)

    # Critical chain: walk back from the last-finishing task via drivers.
    end = max(tasks.values(), key=lambda t: (t.finish, t.kind == "milestone"))
    node = end.id
    while node:
        t = tasks[node]
        if t.done:
            break
        t.critical = True
        node = t.driver


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def ts(day: dt.date, frac: float) -> str:
    minutes = round(frac * DAY_HOURS * 60)
    h, m = divmod(DAY_START_HOUR * 60 + minutes, 60)
    return f"{day.isoformat()} {h:02d}:{m:02d}"


def fmt_day(d: dt.date) -> str:
    return d.strftime("%a %b %d, %Y").replace(" 0", " ")


def clean(name: str) -> str:
    # characters that break mermaid task lines
    return name.replace(":", " ").replace("#", "").replace(";", ",")


def mermaid(cfg: dict, tasks: dict[str, Task]) -> str:
    proj = cfg["project"]
    as_of = to_date(proj["as_of"])
    lines = [
        "gantt",
        f"    title {clean(proj['title'])} - projected as of {as_of.isoformat()}",
        "    dateFormat YYYY-MM-DD HH:mm",
        "    axisFormat %b %y",
        "    tickInterval 1month",
        "    todayMarker off",
    ]
    # Completed work is listed in the task table, not drawn, so the chart
    # starts at the status date.
    sections: dict[str, list[Task]] = {}
    for t in tasks.values():
        if not t.done:
            sections.setdefault(t.section, []).append(t)
    for sec, items in sections.items():
        lines.append(f"    section {clean(sec)}")
        for t in sorted(items, key=lambda t: t.start):
            tags = []
            label = clean(t.name)
            if t.kind in ("inspection", "milestone"):
                tags.append("milestone")
            if t.kind == "work" and t.progress > 0:
                label += f" ({round(t.progress * 100)}%)"
                tags.append("active")
            if t.critical:
                tags.append("crit")
            tagstr = (", ".join(tags) + ", ") if tags else ""
            if t.kind in ("inspection", "milestone"):
                lines.append(f"    {label} :{tagstr}{t.id}, {ts(*t.start)}, 0d")
            else:
                lines.append(f"    {label} :{tagstr}{t.id}, {ts(*t.start)}, {ts(*t.finish)}")
    return "\n".join(lines)


def crew_name(cfg, c):
    return cfg["crews"][c].get("name", c) if c else "-"


def task_rows(cfg, tasks):
    rows = []
    for t in tasks.values():
        if t.kind == "work":
            kind = crew_name(cfg, t.crew)
            pct = f"{round(t.progress * 100)}%"
            rem = f"{t.remaining:.2f}".rstrip("0").rstrip(".")
        else:
            kind = {"lag": "wait", "inspection": "inspection", "milestone": "milestone"}[t.kind]
            pct, rem = "", ""
        s = fmt_day(t.start[0]) if t.start and not t.done else ""
        f = "done" if t.done else fmt_day(t.finish[0])
        rows.append(dict(id=t.id, name=t.name, crew=kind, pct=pct, rem=rem,
                         start=s, finish=f, crit=t.critical, kind=t.kind))
    return rows


def render_md(cfg: dict, tasks: dict[str, Task], gantt: str) -> str:
    proj = cfg["project"]
    as_of = to_date(proj["as_of"])
    keys = [t for t in tasks.values() if t.kind in ("inspection", "milestone")]
    out = [
        f"# {proj['title']} - Schedule",
        "",
        "<!-- GENERATED by gen/generate.py from schedule.yaml - do not edit by hand. -->",
        "",
        f"Status as of **{fmt_day(as_of)}**. Dates after that are projections.",
        "",
        "| Milestone | Projected |",
        "|---|---|",
    ]
    for t in keys:
        out.append(f"| {t.name} | {fmt_day(t.finish[0])} |")
    out += ["", "Interactive view with room-by-room checklists: `docs/index.html`.", ""]
    out += ["```mermaid", gantt, "```", ""]
    out += ["## Remaining work by crew", "", "| Crew | Work days remaining |", "|---|---|"]
    for c in cfg["crews"]:
        rem = sum(t.remaining for t in tasks.values() if t.crew == c and t.kind == "work")
        out.append(f"| {crew_name(cfg, c)} | {rem:.1f} |")
    out += ["", "## Tasks", "",
            "Critical-path tasks (the chain that sets the move-in date) are marked **bold**.", "",
            "| ID | Task | Crew | Done | Days left | Start | Finish |",
            "|---|---|---|---|---|---|---|"]
    for r in task_rows(cfg, tasks):
        name = f"**{r['name']}**" if r["crit"] else r["name"]
        out.append(f"| `{r['id']}` | {name} | {r['crew']} | {r['pct']} | {r['rem']} | {r['start']} | {r['finish']} |")
    out.append("")
    return "\n".join(out)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remodel Schedule</title>
<style>
:root {
  --bg: #fbfaf8; --fg: #1d1d1b; --muted: #6b6a66; --line: #e3e1dc; --card: #ffffff;
  --done: #3f8f5a; --part: #c9902c; --todo: #d9d6cf; --accent: #3a5fa8;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161615; --fg: #ecebe7; --muted: #9c9a94; --line: #2e2d2a; --card: #1f1e1c;
    --done: #5fb27a; --part: #e0aa48; --todo: #3a3935; --accent: #7d9fe0;
  }
}
:root[data-theme="dark"] {
  --bg: #161615; --fg: #ecebe7; --muted: #9c9a94; --line: #2e2d2a; --card: #1f1e1c;
  --done: #5fb27a; --part: #e0aa48; --todo: #3a3935; --accent: #7d9fe0;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1400px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 36px 0 12px; }
.sub { color: var(--muted); margin: 0 0 20px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
.tile .k { color: var(--muted); font-size: .8rem; }
.tile .v { font-weight: 600; font-variant-numeric: tabular-nums; }
.gantt { background: #fff; border: 1px solid var(--line); border-radius: 10px; padding: 8px;
  overflow-x: auto; margin-top: 12px; }
.gantt svg { max-width: none !important; }
details { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  margin: 8px 0; padding: 0 14px; }
summary { cursor: pointer; padding: 10px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
summary .nm { font-weight: 600; flex: 1 1 260px; }
summary .meta { color: var(--muted); font-size: .85rem; font-variant-numeric: tabular-nums; }
.bar { width: 120px; height: 8px; background: var(--todo); border-radius: 4px; overflow: hidden; flex: none; }
.bar > i { display: block; height: 100%; background: var(--done); }
.crit { color: #c0392b; font-size: .75rem; font-weight: 700; letter-spacing: .04em; }
.grid { overflow-x: auto; padding-bottom: 12px; }
table { border-collapse: collapse; font-size: .85rem; }
th, td { padding: 4px 8px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
th { color: var(--muted); font-weight: 500; }
td.c { text-align: center; }
.s { display: inline-block; width: 14px; height: 14px; border-radius: 3px; vertical-align: middle; }
.s.done { background: var(--done); } .s.part { background: var(--part); } .s.todo { background: var(--todo); }
.legend { color: var(--muted); font-size: .85rem; display: flex; gap: 16px; flex-wrap: wrap; }
.filter { margin: 8px 0 4px; display: flex; gap: 8px; flex-wrap: wrap; }
.filter button { background: var(--card); color: var(--fg); border: 1px solid var(--line);
  border-radius: 999px; padding: 4px 12px; cursor: pointer; font: inherit; font-size: .85rem; }
.filter button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
</style>
</head>
<body>
<main>
<h1>__TITLE__</h1>
<p class="sub">Status as of __ASOF__. Later dates are projections. Generated from schedule.yaml.</p>
<div class="tiles">__TILES__</div>

<h2>Gantt</h2>
<div class="gantt"><pre class="mermaid">
__GANTT__
</pre></div>

<h2>Progress by task</h2>
<div class="legend"><span><i class="s done"></i> done</span><span><i class="s part"></i> partial</span><span><i class="s todo"></i> to do</span></div>
<div class="filter" id="filter"></div>
<div id="tasks"></div>
</main>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script>
const DATA = __DATA__;
mermaid.initialize({ startOnLoad: true, theme: "default",
  gantt: { useWidth: Math.max(1200, DATA.tasks.length * 22), barHeight: 18, fontSize: 12 } });

const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const crews = ["All", ...new Set(DATA.tasks.filter(t => t.kind === "work").map(t => t.crew))];
let sel = "All";
function drawFilter() {
  document.getElementById("filter").innerHTML = crews.map(c =>
    `<button aria-pressed="${c === sel}" data-c="${esc(c)}">${esc(c)}</button>`).join("");
}
document.getElementById("filter").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  sel = b.dataset.c; drawFilter(); drawTasks();
});
function cell(p) {
  if (p === undefined) return "<td></td>";
  const cls = p >= 1 ? "done" : p > 0 ? "part" : "todo";
  const tip = p >= 1 ? "done" : p > 0 ? Math.round(p * 100) + "%" : "to do";
  return `<td class="c" title="${tip}"><i class="s ${cls}"></i></td>`;
}
function drawTasks() {
  const html = DATA.tasks.filter(t => t.kind === "work" && (sel === "All" || t.crew === sel)).map(t => {
    let grid = "";
    if (t.items.length) {
      const cols = [...new Set(t.items.map(i => i.item))];
      const rows = [...new Set(t.items.map(i => i.area))];
      const m = {}; t.items.forEach(i => m[i.area + "|" + i.item] = i.progress);
      grid = `<div class="grid"><table><tr><th>Area</th>${cols.map(c => `<th>${esc(c.replaceAll("_", " "))}</th>`).join("")}</tr>` +
        rows.map(r => `<tr><td>${esc(DATA.areas[r])}</td>${cols.map(c => cell(m[r + "|" + c])).join("")}</tr>`).join("") +
        `</table></div>`;
    }
    return `<details><summary><span class="nm">${esc(t.name)}</span>` +
      (t.crit ? `<span class="crit">CRITICAL</span>` : "") +
      `<span class="bar"><i style="width:${Math.round(t.progress * 100)}%"></i></span>` +
      `<span class="meta">${esc(t.crew)} · ${Math.round(t.progress * 100)}% · ${t.rem} days left · ${t.done ? "done" : esc(t.start) + " → " + esc(t.finish)}</span>` +
      `</summary>${grid}</details>`;
  }).join("");
  document.getElementById("tasks").innerHTML = html;
}
drawFilter(); drawTasks();
</script>
</body>
</html>
"""


def render_html(cfg: dict, tasks: dict[str, Task], gantt: str) -> str:
    proj = cfg["project"]
    rows = {r["id"]: r for r in task_rows(cfg, tasks)}
    data = {
        "areas": {a: v["name"] for a, v in cfg["areas"].items()},
        "tasks": [
            dict(id=t.id, name=t.name, kind=t.kind, crew=rows[t.id]["crew"],
                 progress=round(t.progress, 3), rem=rows[t.id]["rem"], done=t.done,
                 start=rows[t.id]["start"], finish=rows[t.id]["finish"], crit=t.critical,
                 items=[dict(area=i.area, item=i.item, progress=i.progress) for i in t.items])
            for t in tasks.values()
        ],
    }
    tiles = []
    for t in tasks.values():
        if t.kind in ("inspection", "milestone"):
            tiles.append(f'<div class="tile"><div class="k">{html.escape(t.name)}</div>'
                         f'<div class="v">{html.escape(fmt_day(t.finish[0]))}</div></div>')
    return (HTML_TEMPLATE
            .replace("__TITLE__", html.escape(proj["title"]))
            .replace("__ASOF__", html.escape(fmt_day(to_date(proj["as_of"]))))
            .replace("__TILES__", "".join(tiles))
            .replace("__GANTT__", html.escape(gantt))
            .replace("__DATA__", json.dumps(data).replace("</", "<\\/")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if outputs are out of date")
    args = ap.parse_args()
    try:
        cfg = load(SOURCE)
        tasks = build_tasks(cfg)
        schedule(cfg, tasks)
    except ScheduleError as e:
        print(f"schedule.yaml: {e}", file=sys.stderr)
        return 2
    gantt = mermaid(cfg, tasks)
    outputs = {OUT_MD: render_md(cfg, tasks, gantt), OUT_HTML: render_html(cfg, tasks, gantt)}
    if args.check:
        stale = [p for p, txt in outputs.items() if not p.exists() or p.read_text() != txt]
        for p in stale:
            print(f"out of date: {p.relative_to(ROOT)} (run python3 gen/generate.py)", file=sys.stderr)
        return 1 if stale else 0
    for p, txt in outputs.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt)
    end = max(tasks.values(), key=lambda t: (t.finish, t.kind == "milestone"))
    print(f"wrote {OUT_MD.relative_to(ROOT)}, {OUT_HTML.relative_to(ROOT)}; "
          f"{end.name}: {fmt_day(end.finish[0])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
