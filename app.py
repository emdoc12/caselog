#!/usr/bin/env python3
"""
caselog — physician reviewer case & earnings tracker

Logs time per case, computes pay against each review organization's contract
rate schedule, and reports true effective hourly rate by organization and
case type.

Everything is configured in the web UI: app title, tax reserve, and each
review organization's case types, rate periods, contract year and payment
lag. Nothing needs to be set through Docker.

NO PHI. Stores dates, organization, case type, counts, minutes, and free-text
notes only. Never enter patient identifiers, case numbers tied to
beneficiaries, or clinical detail.
"""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from calendar import Calendar, month_name
from datetime import date, datetime, timedelta

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, send_file, url_for)

__version__ = "1.5.0"

DB_PATH = os.environ.get("CASELOG_DB", "/data/caselog.db")

app = Flask(__name__)
app.secret_key = os.environ.get("CASELOG_SECRET", "caselog-local")

# ------------------------------------------------------------------ defaults
DEFAULT_SETTINGS = {
    "title": "caselog",
    "tax_pct": "30",
}

DEFAULT_ORGS = {
    "org1": {
        "label": "Review Organization",
        "payment_lag_months": 2,
        "annual_cap": 0,
        "fiscal_year_start_month": 1,
        "case_types": {
            "review": {"label": "Case Review", "basis": "case"},
            "hourly": {"label": "Hourly Work", "basis": "hour"},
        },
        "rate_periods": [
            {"start": "2026-01-01", "end": "2026-12-31",
             "case_rates": {"review": 50}, "hourly": 100.0},
        ],
    }
}


# ------------------------------------------------------------------ database
def db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            work_date  TEXT    NOT NULL,
            org        TEXT    NOT NULL DEFAULT 'org1',
            case_type  TEXT    NOT NULL,
            qty        INTEGER NOT NULL DEFAULT 1,
            minutes    REAL    NOT NULL,
            notes      TEXT,
            created_at TEXT    NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    if "org" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN org TEXT NOT NULL DEFAULT 'org1'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON entries(work_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_org ON entries(org)")

    # migrate a legacy orgs.json into settings, then seed defaults
    have = conn.execute("SELECT value FROM settings WHERE key='orgs'").fetchone()
    if not have:
        legacy = os.path.join(os.path.dirname(DB_PATH) or ".", "orgs.json")
        seed = DEFAULT_ORGS
        if os.path.exists(legacy):
            try:
                with open(legacy) as fh:
                    loaded = json.load(fh)
                if loaded:
                    seed = loaded
            except (OSError, json.JSONDecodeError):
                pass
        conn.execute("INSERT INTO settings (key,value) VALUES ('orgs',?)",
                     (json.dumps(seed),))
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(key, default=""):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db().execute("INSERT INTO settings (key,value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db().commit()


def load_orgs():
    try:
        orgs = json.loads(get_setting("orgs", "{}"))
    except json.JSONDecodeError:
        orgs = {}
    return orgs or DEFAULT_ORGS


def save_orgs(orgs):
    set_setting("orgs", json.dumps(orgs, indent=2))


def tax_pct():
    try:
        return float(get_setting("tax_pct", "30"))
    except ValueError:
        return 30.0


# ----------------------------------------------------------------- rate math
def org_cfg(orgs, key):
    return orgs.get(key) or next(iter(orgs.values()))


def rates_on(cfg, day):
    periods = cfg.get("rate_periods") or []
    for p in periods:
        if p.get("start", "0000-01-01") <= day <= p.get("end", "9999-12-31"):
            return p.get("case_rates", {}), float(p.get("hourly", 0) or 0)
    if periods:
        last = periods[-1]
        return last.get("case_rates", {}), float(last.get("hourly", 0) or 0)
    return {}, 0.0


def pay_for(cfg, case_type, qty, minutes, day):
    case_rates, hourly = rates_on(cfg, day)
    basis = (cfg.get("case_types", {}).get(case_type) or {}).get("basis", "hour")
    if basis == "case":
        return qty * float(case_rates.get(case_type, 0) or 0)
    return (minutes / 60.0) * hourly


def fiscal_year(cfg, day):
    m0 = int(cfg.get("fiscal_year_start_month") or 1)
    d = datetime.strptime(day, "%Y-%m-%d").date()
    y = d.year if d.month >= m0 else d.year - 1
    start, end = date(y, m0, 1), date(y + 1, m0, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), (f"{y}–{y + 1}" if m0 != 1 else str(y))


def payment_month(cfg, day):
    lag = int(cfg.get("payment_lag_months") or 0)
    d = datetime.strptime(day, "%Y-%m-%d").date()
    m = d.month + lag
    return f"{month_name[(m - 1) % 12 + 1]} {d.year + (m - 1) // 12}"


def enrich(orgs, rows):
    out = []
    for r in rows:
        d = dict(r)
        cfg = org_cfg(orgs, d["org"])
        ct = cfg.get("case_types", {}).get(d["case_type"], {})
        d["org_label"] = cfg.get("label", d["org"])
        d["label"] = ct.get("label", d["case_type"])
        d["basis"] = ct.get("basis", "hour")
        d["pay"] = pay_for(cfg, d["case_type"], d["qty"], d["minutes"], d["work_date"])
        d["hours"] = d["minutes"] / 60.0
        d["min_per_case"] = d["minutes"] / d["qty"] if d["qty"] else 0
        d["eff_rate"] = d["pay"] / d["hours"] if d["hours"] else 0
        d["pay_month"] = payment_month(cfg, d["work_date"])
        out.append(d)
    return out


def totals(rows, pct=None):
    pct = tax_pct() if pct is None else pct
    cases = sum(r["qty"] for r in rows)
    minutes = sum(r["minutes"] for r in rows)
    pay = sum(r["pay"] for r in rows)
    hours = minutes / 60.0
    return {"cases": cases, "minutes": minutes, "hours": hours, "pay": pay,
            "days": len({r["work_date"] for r in rows}),
            "eff_rate": pay / hours if hours else 0,
            "min_per_case": minutes / cases if cases else 0,
            "tax": pay * pct / 100.0, "net": pay * (1 - pct / 100.0)}


# -------------------------------------------------------------------- routes
_HERE = os.path.dirname(os.path.abspath(__file__))
# In the image the icon sits beside app.py; running from a source checkout it is
# still under docs/.
# iOS caches home-screen icons by URL, in a system cache that survives deleting
# the shortcut, clearing Safari and rebooting. Serving the icon from a path that
# changes with its contents is the only reliable way to make a new icon appear.
def _asset(name):
    """Icon files sit beside app.py in the image, under docs/ in a checkout."""
    return next((p for p in (os.path.join(_HERE, name),
                             os.path.join(_HERE, "docs", name))
                 if os.path.exists(p)), None)


def _tag(path):
    if not path:
        return "none"
    return hashlib.md5(open(path, "rb").read()).hexdigest()[:10]


ICON_PNG = _asset("icon-180.png")
FAVICON_PNG = _asset("favicon-180.png")
ICON_TAG = _tag(ICON_PNG)
ICON_HREF = f"/apple-touch-icon-{ICON_TAG}.png"
FAVICON_HREF = f"/favicon-{_tag(FAVICON_PNG)}.png"


@app.context_processor
def inject_icon():
    return {"icon_href": ICON_HREF, "favicon_href": FAVICON_HREF}


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
@app.route("/apple-touch-icon-<tag>.png")
def apple_touch_icon(tag=None):
    """iOS home-screen icon. Safari ignores SVG here, so this must be a PNG."""
    if not ICON_PNG:
        return Response(status=404)
    # max_age=0 with conditional revalidation: Safari re-checks every time and
    # gets a cheap 304 when nothing changed. A long max-age here means a
    # changed icon stays invisible for the whole cache lifetime, and clearing
    # it needs more than deleting the home-screen shortcut.
    return send_file(ICON_PNG, mimetype="image/png",
                     max_age=0, conditional=True)


@app.route("/favicon.png")
@app.route("/favicon-<tag>.png")
def favicon(tag=None):
    if not FAVICON_PNG:
        return Response(status=404)
    return send_file(FAVICON_PNG, mimetype="image/png",
                     max_age=0, conditional=True)


@app.route("/")
def index():
    orgs = load_orgs()
    pct = tax_pct()
    org_filter = request.args.get("org") or "all"
    if org_filter != "all" and org_filter not in orgs:
        org_filter = "all"

    ym = request.args.get("m") or date.today().strftime("%Y-%m")
    try:
        year, mon = (int(x) for x in ym.split("-"))
        date(year, mon, 1)
    except (ValueError, TypeError):
        year, mon = date.today().year, date.today().month
        ym = f"{year:04d}-{mon:02d}"

    conn = db()
    if org_filter == "all":
        month_rows = enrich(orgs, conn.execute(
            "SELECT * FROM entries WHERE work_date LIKE ? ORDER BY work_date DESC, id DESC",
            (f"{ym}-%",)).fetchall())
        all_rows = enrich(orgs, conn.execute("SELECT * FROM entries").fetchall())
    else:
        month_rows = enrich(orgs, conn.execute(
            "SELECT * FROM entries WHERE work_date LIKE ? AND org=? "
            "ORDER BY work_date DESC, id DESC", (f"{ym}-%", org_filter)).fetchall())
        all_rows = enrich(orgs, conn.execute(
            "SELECT * FROM entries WHERE org=?", (org_filter,)).fetchall())

    by_day, mins_by_day = {}, {}
    for r in month_rows:
        by_day[r["work_date"]] = by_day.get(r["work_date"], 0) + r["qty"]
        mins_by_day[r["work_date"]] = mins_by_day.get(r["work_date"], 0) + r["minutes"]
    peak = max(by_day.values()) if by_day else 0
    weeks = [[{
        "day": d.day, "in_month": d.month == mon, "today": d == date.today(),
        "count": by_day.get(d.isoformat(), 0),
        "minutes": mins_by_day.get(d.isoformat(), 0),
        "level": (0 if not by_day.get(d.isoformat()) else
                  min(5, 1 + int(4 * (by_day[d.isoformat()] - 1) / max(peak - 1, 1)))),
    } for d in week] for week in Calendar(firstweekday=6).monthdatescalendar(year, mon)]

    org_rows = []
    for key in orgs:
        sel = [r for r in month_rows if r["org"] == key]
        if sel:
            t = totals(sel, pct)
            t.update(key=key, label=orgs[key].get("label", key))
            org_rows.append(t)
    org_rows.sort(key=lambda x: -x["pay"])

    breakdown = []
    for okey, ckey in sorted({(r["org"], r["case_type"]) for r in month_rows}):
        sel = [r for r in month_rows if r["org"] == okey and r["case_type"] == ckey]
        t = totals(sel, pct)
        t.update(label=sel[0]["label"], basis=sel[0]["basis"], org_label=sel[0]["org_label"])
        breakdown.append(t)
    breakdown.sort(key=lambda x: -x["pay"])

    cy = None
    if org_filter != "all":
        cfg = orgs[org_filter]
        s, e, lab = fiscal_year(cfg, f"{ym}-15")
        cy = totals([r for r in all_rows if s <= r["work_date"] <= e], pct)
        cy.update(label=lab, cap=float(cfg.get("annual_cap") or 0))

    ref_key = org_filter if org_filter != "all" else next(iter(orgs))
    ref = org_cfg(orgs, ref_key)
    case_rates, hourly = rates_on(ref, f"{ym}-15")

    return render_template(
        "index.html", version=__version__, title=get_setting("title", "caselog"),
        ym=ym, month_label=f"{month_name[mon]} {year}",
        prev_m=(date(year, mon, 1) - timedelta(days=1)).strftime("%Y-%m"),
        next_m=(date(year, mon, 28) + timedelta(days=7)).strftime("%Y-%m"),
        today=date.today().isoformat(),
        rows=month_rows, m=totals(month_rows, pct), alltime=totals(all_rows, pct),
        weeks=weeks, breakdown=breakdown, org_rows=org_rows, cy=cy,
        orgs=orgs, org_filter=org_filter, default_org=ref_key,
        ref_label=ref.get("label", ""), case_rates=case_rates, hourly=hourly,
        tax_pct=pct, pay_month=payment_month(ref, f"{ym}-01"))


MAX_LAPS = 60


def parse_laps(raw):
    """Timer splits as [(minutes, note)].

    Accepts either bare numbers or {"m": minutes, "n": note} objects, so a page
    served before per-lap notes existed still posts successfully. Returns [] for
    anything malformed.
    """
    if not raw:
        return []
    try:
        vals = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(vals, list):
        return []
    out = []
    for v in vals[:MAX_LAPS]:
        note = ""
        if isinstance(v, dict):
            note = str(v.get("n") or "").strip()[:280]
            v = v.get("m")
        try:
            m = round(float(v), 2)
        except (TypeError, ValueError):
            continue
        if m > 0:
            out.append((m, note))
    return out


@app.route("/add", methods=["POST"])
def add():
    orgs = load_orgs()
    f = request.form
    work_date = f.get("work_date") or date.today().isoformat()
    org = f.get("org") if f.get("org") in orgs else next(iter(orgs))
    case_type = f.get("case_type", "")
    back = dict(m=work_date[:7], org=request.args.get("org") or "all")
    try:
        qty = max(1, int(f.get("qty") or 1))
        minutes = float(f.get("minutes") or 0)
    except ValueError:
        flash("Cases and minutes must be numbers.", "error")
        return redirect(url_for("index", **back))
    if case_type not in orgs[org].get("case_types", {}):
        flash("Pick a case type and enter minutes greater than zero.", "error")
        return redirect(url_for("index", **back))

    # A timer run with more than one lap becomes one row per case, so per-case
    # variance survives. Merging in the UI clears laps and falls through to the
    # single-row path below with qty set to the case count.
    laps = parse_laps(f.get("laps"))
    if len(laps) > 1:
        now = datetime.now().isoformat(timespec="seconds")
        shared = (f.get("notes") or "").strip()[:280]
        db().executemany(
            "INSERT INTO entries (work_date, org, case_type, qty, minutes, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            [(work_date, org, case_type, 1, m, note or shared, now)
             for m, note in laps])
        db().commit()
        flash(f"Logged {len(laps)} cases from the timer.", "ok")
        return redirect(url_for("index", **back))

    if minutes <= 0:
        flash("Pick a case type and enter minutes greater than zero.", "error")
        return redirect(url_for("index", **back))

    db().execute(
        "INSERT INTO entries (work_date, org, case_type, qty, minutes, notes, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (work_date, org, case_type, qty, minutes,
         (f.get("notes") or "").strip()[:280],
         datetime.now().isoformat(timespec="seconds")))
    db().commit()
    return redirect(url_for("index", **back))


@app.route("/note/<int:entry_id>", methods=["POST"])
def set_note(entry_id):
    """Edit one entry's note in place. The timer writes a row per case, so this
    is how a batch that was logged without notes gets labelled afterwards."""
    row = db().execute("SELECT work_date FROM entries WHERE id=?", (entry_id,)).fetchone()
    if row:
        db().execute("UPDATE entries SET notes=? WHERE id=?",
                     ((request.form.get("notes") or "").strip()[:280], entry_id))
        db().commit()
    m = row["work_date"][:7] if row else date.today().isoformat()[:7]
    return redirect(url_for("index", m=m, org=request.args.get("org") or "all"))


@app.route("/delete/<int:entry_id>", methods=["POST"])
def delete(entry_id):
    row = db().execute("SELECT work_date FROM entries WHERE id=?", (entry_id,)).fetchone()
    db().execute("DELETE FROM entries WHERE id=?", (entry_id,))
    db().commit()
    return redirect(url_for("index", m=(row["work_date"][:7] if row else None),
                            org=request.args.get("org") or "all"))


# ------------------------------------------------------------------ settings
@app.route("/settings")
def settings():
    orgs = load_orgs()
    counts = {r["org"]: r["n"] for r in db().execute(
        "SELECT org, COUNT(*) n, SUM(qty) cases FROM entries GROUP BY org")}
    cases = {r["org"]: r["cases"] for r in db().execute(
        "SELECT org, SUM(qty) cases FROM entries GROUP BY org")}
    return render_template("settings.html", version=__version__,
                           title=get_setting("title", "caselog"),
                           tax_pct=tax_pct(), orgs=orgs,
                           entry_counts=counts, case_counts=cases,
                           months=list(month_name)[1:])


@app.route("/settings/general", methods=["POST"])
def settings_general():
    title = (request.form.get("title") or "caselog").strip()[:60] or "caselog"
    set_setting("title", title)
    try:
        pct = min(90.0, max(0.0, float(request.form.get("tax_pct") or 30)))
        set_setting("tax_pct", pct)
    except ValueError:
        flash("Tax reserve must be a number.", "error")
    flash("Settings saved.", "ok")
    return redirect(url_for("settings"))


def validate_org(payload, key=None):
    """Return (key, cfg) or raise ValueError with a readable message."""
    label = (payload.get("label") or "").strip()
    if not label:
        raise ValueError("Organization needs a name.")
    key = key or re.sub(r"[^a-z0-9]+", "", label.lower())[:24] or "org"

    types = {}
    for t in payload.get("case_types") or []:
        tkey = re.sub(r"[^a-z0-9]+", "", str(t.get("key", "")).lower())[:24]
        tlabel = (t.get("label") or "").strip()
        basis = "case" if t.get("basis") == "case" else "hour"
        if not tkey or not tlabel:
            continue
        types[tkey] = {"label": tlabel, "basis": basis}
    if not types:
        raise ValueError("Add at least one case type.")

    periods = []
    for p in payload.get("rate_periods") or []:
        start, end = (p.get("start") or "").strip(), (p.get("end") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", start) or not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
            raise ValueError("Rate period dates must be YYYY-MM-DD.")
        if end < start:
            raise ValueError(f"Rate period ending {end} starts after it ends.")
        rates = {}
        for tkey, cfg in types.items():
            if cfg["basis"] == "case":
                try:
                    rates[tkey] = float(((p.get("case_rates") or {}).get(tkey)) or 0)
                except (TypeError, ValueError):
                    rates[tkey] = 0.0
        try:
            hourly = float(p.get("hourly") or 0)
        except (TypeError, ValueError):
            hourly = 0.0
        periods.append({"start": start, "end": end, "case_rates": rates, "hourly": hourly})
    if not periods:
        raise ValueError("Add at least one rate period.")
    periods.sort(key=lambda x: x["start"])

    def as_int(name, lo, hi, default):
        try:
            return min(hi, max(lo, int(float(payload.get(name) or default))))
        except (TypeError, ValueError):
            return default

    return key, {
        "label": label,
        "payment_lag_months": as_int("payment_lag_months", 0, 12, 0),
        "annual_cap": max(0.0, float(payload.get("annual_cap") or 0)),
        "fiscal_year_start_month": as_int("fiscal_year_start_month", 1, 12, 1),
        "case_types": types,
        "rate_periods": periods,
    }


@app.route("/settings/org", methods=["POST"])
def settings_org():
    orgs = load_orgs()
    existing = request.form.get("key") or None
    try:
        payload = json.loads(request.form.get("payload") or "{}")
        key, cfg = validate_org(payload, existing)
    except (json.JSONDecodeError, ValueError) as exc:
        flash(str(exc) or "Could not read that organization.", "error")
        return redirect(url_for("settings"))

    if not existing:
        base, n = key, 2
        while key in orgs:
            key, n = f"{base}{n}", n + 1
    orgs[key] = cfg
    save_orgs(orgs)
    flash(f"Saved {cfg['label']}.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/org/<key>/delete", methods=["POST"])
def settings_org_delete(key):
    orgs = load_orgs()
    if len(orgs) <= 1:
        flash("Keep at least one organization.", "error")
        return redirect(url_for("settings"))
    if key in orgs:
        label = orgs[key].get("label", key)
        if request.form.get("purge") == "yes":
            db().execute("DELETE FROM entries WHERE org=?", (key,))
            db().commit()
        del orgs[key]
        save_orgs(orgs)
        flash(f"Removed {label}.", "ok")
    return redirect(url_for("settings"))


@app.route("/export.csv")
def export_csv():
    orgs = load_orgs()
    rows = enrich(orgs, db().execute("SELECT * FROM entries ORDER BY work_date, id").fetchall())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "organization", "case_type", "basis", "cases", "minutes",
                "min_per_case", "gross_pay", "effective_hourly", "expected_payment", "notes"])
    for r in rows:
        w.writerow([r["work_date"], r["org_label"], r["label"], r["basis"], r["qty"],
                    round(r["minutes"], 1), round(r["min_per_case"], 1),
                    f'{r["pay"]:.2f}', f'{r["eff_rate"]:.2f}', r["pay_month"], r["notes"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename=caselog_{date.today().isoformat()}.csv'})


@app.route("/healthz")
def healthz():
    return {"ok": True, "version": __version__, "icon": ICON_HREF,
            "favicon": FAVICON_HREF,
            "orgs": list(load_orgs())}


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
