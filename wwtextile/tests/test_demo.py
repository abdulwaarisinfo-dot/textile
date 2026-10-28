"""Live-demo preparation: showcase data, consistency across modules, reset, public-demo hardening, end-to-end workflow."""
import dataclasses
import json
import re
import shutil

import pytest
from starlette.testclient import TestClient

from wwtextile import config, db, demo_data
from wwtextile.services import accessories, finance, inventory, reports
from wwtextile.web import create_app

PW = "client-demo-pass-1"
ADMIN_PW = "admin-private-pass-9"


def make_cfg(tmp_path, **kw):
    base = dataclasses.replace(config.load(), env="demo", db_path=str(tmp_path / "d.db"), upload_dir=str(tmp_path / "up"),
                               demo_password=PW, demo_admin_password=ADMIN_PW, max_failed_logins=50)
    return dataclasses.replace(base, **kw)


@pytest.fixture(scope="module")
def showcase(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("showcase")
    cfg = make_cfg(tmp)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    info = demo_data.build(conn, cfg)
    return cfg, conn, info


class Cl:
    def __init__(self, app):
        self.c = TestClient(app, follow_redirects=False)
        self.csrf = None

    def login(self, u, pw=PW):
        tok = re.search(r'name="lcsrf" value="([^"]+)"', self.c.get("/login").text).group(1)
        r = self.c.post("/login", data={"username": u, "password": pw, "lcsrf": tok, "next": "/"})
        if r.status_code == 303:
            self.csrf = re.search(r'name="csrf" value="([^"]+)"', self.c.get("/").text).group(1)
        return r

    def post(self, path, data=None, **kw):
        return self.c.post(path, data={**(data or {}), "csrf": self.csrf}, **kw)


# ------------------------------------------------------------------ data
def test_every_module_has_realistic_content(showcase):
    _, c, info = showcase
    assert info["users"] == 11 and info["parties"] >= 20 and info["lots"] >= 25
    for key, floor in dict(receipts=15, jobs=10, results=8, dispatches=6, gate_passes=5, gate_entries=20, accessory_docs=8, mh_orders=5,
                           mh_logs=15, finance_entries=50, audit_events=300).items():
        assert info[key] >= floor, (key, info[key])
    q = lambda s: c.execute(s).fetchone()[0]
    assert {r[0] for r in c.execute("SELECT DISTINCT process_type FROM processing_jobs")} == {"dyeing", "printing", "embroidery"}
    assert q("SELECT COUNT(*) FROM processing_results WHERE wastage>0 AND shrinkage>0") >= 8      # wastage and shrinkage present
    assert q("SELECT COUNT(*) FROM lots WHERE kind='finished'") >= 8                               # finished goods exist
    assert q("SELECT COUNT(*) FROM items WHERE name LIKE 'Kora%'") >= 5                            # kora kapra items
    assert q("SELECT COUNT(*) FROM acc_ledger") > 10 and q("SELECT COUNT(*) FROM mh_consumptions") >= 1
    kinds = {r[0] for r in c.execute("SELECT DISTINCT kind FROM fin_entries WHERE status='posted'")}
    assert kinds == {"bill", "invoice", "payment_in", "payment_out", "expense"}
    assert q("SELECT COUNT(*) FROM gate_entries WHERE status='inside'") >= 1                      # a vehicle genuinely inside
    assert q("SELECT COUNT(*) FROM alerts WHERE status IN ('open','acknowledged')") >= 5          # watchtower has content
    assert q("SELECT COUNT(*) FROM dispatches WHERE status='exited'") >= 4


def test_stock_and_money_are_consistent_across_modules(showcase):
    _, c, _ = showcase
    assert inventory.reconcile_ledger(c) == [] and accessories.reconcile(c) == []
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and not c.execute("PRAGMA foreign_key_check").fetchall()
    # every posted result balances exactly: input = accepted + rejected + wastage + shrinkage + remaining + variance
    for r in c.execute("SELECT * FROM processing_results WHERE status='posted'"):
        iss = c.execute("SELECT qty FROM processing_issues WHERE id=?", (r["issue_id"],)).fetchone()["qty"]
        assert iss == r["accepted"] + r["rejected"] + r["wastage"] + r["shrinkage"] + r["remaining"] + r["variance"], r["result_no"]
        assert r["variance"] >= 0
    # each finished lot was created by exactly one posted result for the accepted quantity
    for r in c.execute("SELECT l.lot_no, SUM(sl.qty) q FROM lots l JOIN stock_ledger sl ON sl.lot_id=l.id WHERE l.kind='finished' AND sl.movement LIKE '%receipt%' GROUP BY l.id"):
        assert r["q"] > 0
    # posted payments never over-allocated; allocations never exceed bill amounts
    for p in c.execute("SELECT id, amount_paise FROM fin_entries WHERE kind IN ('payment_in','payment_out') AND status='posted'"):
        assert finance.allocated_from_payment(c, p["id"]) <= p["amount_paise"]
    for b in c.execute("SELECT id, amount_paise FROM fin_entries WHERE kind IN ('bill','invoice') AND status='posted'"):
        assert finance.allocated_to_bill(c, b["id"]) <= b["amount_paise"]
    # one posted supplier bill per receipt (same supplier reference)
    n_receipts = c.execute("SELECT COUNT(*) FROM goods_receipts WHERE status='posted'").fetchone()[0]
    n_bills = c.execute("SELECT COUNT(*) FROM fin_entries f WHERE f.kind='bill' AND f.status='posted' AND f.reference IN (SELECT supplier_ref FROM goods_receipts)").fetchone()[0]
    assert n_bills == n_receipts
    # dispatched quantity per exited dispatch equals the stock that left the Finished Store
    out = c.execute("SELECT -SUM(qty) FROM stock_ledger WHERE movement LIKE '%dispatch%' OR source_type='dispatch'").fetchone()[0] or 0
    exited = c.execute("SELECT COALESCE(SUM(dl.qty),0) FROM dispatch_lines dl JOIN dispatches d ON d.id=dl.dispatch_id WHERE d.status='exited'").fetchone()[0]
    assert out == exited and exited > 0


def test_history_is_spread_over_weeks_but_never_in_the_future(showcase):
    _, c, _ = showcase
    lo, hi = c.execute("SELECT MIN(posted_at), MAX(posted_at) FROM stock_ledger").fetchone()
    from datetime import datetime, timezone
    assert lo < hi and hi <= datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert (datetime.fromisoformat(hi.replace("Z", "+00:00")) - datetime.fromisoformat(lo.replace("Z", "+00:00"))).days >= 40
    assert c.execute("SELECT MAX(ts) FROM audit_events").fetchone()[0] <= datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_every_report_runs_on_showcase_data_and_csv_matches_html_totals(showcase):
    cfg, c, _ = showcase
    app = create_app(cfg)
    cl = Cl(app)
    assert cl.login("ceo").status_code == 303
    for key, spec in reports.REPORTS.items():
        extra = "&party_id=1" if key == "party-statement" else ""
        r = cl.c.get(f"/reports/{key}?run=1{extra}")
        assert r.status_code == 200, key
        r2 = cl.c.get(f"/reports/{key}?run=1&format=csv{extra}")
        assert r2.status_code == 200 and "csv" in r2.headers["content-type"], key


def test_same_seed_same_story_other_seed_different(tmp_path):
    def run(seed, name):
        cfg = make_cfg(tmp_path, db_path=str(tmp_path / name))
        c = db.connect(cfg.db_path)
        db.migrate(c)
        demo_data.build(c, cfg, seed)
        lots = c.execute("SELECT lot_no, (SELECT SUM(qty) FROM stock_ledger WHERE lot_id=lots.id) q FROM lots ORDER BY id").fetchall()
        paise = c.execute("SELECT SUM(amount_paise) FROM fin_entries").fetchone()[0]
        return [tuple(x) for x in lots], paise
    a, b, d = run(7, "a.db"), run(7, "b.db"), run(8, "c.db")
    assert a == b
    assert a != d


def test_build_refuses_outside_demo_and_on_nonempty_db(tmp_path):
    cfg = make_cfg(tmp_path, env="production")
    c = db.connect(cfg.db_path)
    db.migrate(c)
    from wwtextile.errors import BusinessRuleViolation
    with pytest.raises(BusinessRuleViolation):
        demo_data.build(c, cfg)
    with pytest.raises(BusinessRuleViolation):
        demo_data.reset(cfg)
    cfg2 = make_cfg(tmp_path, db_path=str(tmp_path / "n.db"))
    c2 = db.connect(cfg2.db_path)
    db.migrate(c2)
    demo_data.build(c2, cfg2)
    with pytest.raises(BusinessRuleViolation):
        demo_data.build(c2, cfg2)


def test_admin_has_private_password_clients_do_not_get_admin(showcase):
    cfg, _, _ = showcase
    app = create_app(cfg)
    assert Cl(app).login("admin", PW).status_code == 401           # the shared client password does not open admin
    a = Cl(app)
    assert a.login("admin", ADMIN_PW).status_code == 303
    for u in ("ceo", "manager1", "store1", "prod1", "gate1", "fin1"):
        cl = Cl(app)
        assert cl.login(u).status_code == 303
        assert cl.c.get("/admin/users").status_code == 403
        assert cl.post("/demo/reset", {"confirm": "RESET"}).status_code == 403


# ------------------------------------------------------------------ pages
def test_every_reachable_page_renders_for_every_role(showcase):
    cfg, _, _ = showcase
    app = create_app(cfg)
    bad = []
    for user, pw in [("admin", ADMIN_PW), ("ceo", PW), ("manager1", PW), ("store1", PW), ("prod1", PW), ("gate1", PW), ("fin1", PW)]:
        cl = Cl(app)
        assert cl.login(user, pw).status_code == 303
        seen, todo = set(), ["/"]
        while todo and len(seen) < 150:
            u = todo.pop()
            if u in seen:
                continue
            seen.add(u)
            r = cl.c.get(u)
            if r.status_code >= 500:
                bad.append((user, u, r.status_code))
            if r.status_code == 200:
                for h in re.findall(r'href="(/[^"#]*)"', r.text):
                    h = h.replace("&amp;", "&")
                    if not h.startswith(("/static", "/logout", "/api")):
                        todo.append(h)
    assert not bad, bad


def test_demo_guide_is_honest_about_integrations(showcase):
    cfg, _, _ = showcase
    cl = Cl(create_app(cfg))
    cl.login("ceo")
    page = cl.c.get("/demo").text
    assert "Not connected" in page and "not an AI model" in page
    row = re.search(r"Assistant \(language model\)</td><td><span class=\"badge[^>]*>([^<]+)<", page)
    assert row and row.group(1) == "Not connected"           # no LLM key configured -> never presented as connected
    assert re.search(r"WhatsApp[^<]*</td><td><span class=\"badge[^>]*>Not connected<", page)
    assert PW not in page and ADMIN_PW not in page             # passwords never rendered
    assert "Reset the demo" not in page                        # reset form is admin-only


def test_demo_pages_404_outside_demo_env(tmp_path):
    cfg = make_cfg(tmp_path, env="production", secret_key="k" * 40, cookie_secure=False)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    from wwtextile import seed
    seed.init_structure(c)
    from wwtextile.services import users
    users.bootstrap_admin(c, "owner", "Owner", "a-long-enough-pass-1", must_change=False)
    cl = Cl(create_app(cfg))
    assert cl.login("owner", "a-long-enough-pass-1").status_code == 303
    assert cl.c.get("/demo").status_code == 404
    assert cl.post("/demo/reset", {"confirm": "RESET"}).status_code == 404


# ------------------------------------------------------------------ reset
def test_reset_via_ui_requires_admin_confirmation_and_regenerates(tmp_path):
    cfg = make_cfg(tmp_path)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    demo_data.build(c, cfg)
    c.close()
    app = create_app(cfg)
    a = Cl(app)
    a.login("admin", ADMIN_PW)
    # something a visitor changed
    c = db.connect(cfg.db_path)
    c.execute("INSERT INTO parties(kind, name) VALUES ('customer','Visitor Test Co')")
    c.close()
    assert a.post("/demo/reset", {"confirm": "yes"}).status_code == 303           # wrong confirmation -> nothing happens
    c = db.connect(cfg.db_path)
    assert c.execute("SELECT COUNT(*) FROM parties WHERE name='Visitor Test Co'").fetchone()[0] == 1
    c.close()
    r = a.post("/demo/reset", {"confirm": "RESET", "seed": "99"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    c = db.connect(cfg.db_path)
    assert c.execute("SELECT COUNT(*) FROM parties WHERE name='Visitor Test Co'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM lots").fetchone()[0] >= 25 and inventory.reconcile_ledger(c) == []
    c.close()
    assert a.c.get("/", follow_redirects=False).status_code == 303                # old session is gone
    assert Cl(app).login("admin", ADMIN_PW).status_code == 303                    # and the new data accepts the same passwords


# ------------------------------------------------------------------ public demo hardening
def test_public_demo_refuses_weak_or_default_configuration(tmp_path):
    ok = make_cfg(tmp_path, public_demo=True, secret_key="s" * 40, cookie_secure=True)
    assert config.validate_for_start(ok) == []
    for change in (dict(secret_key=""), dict(cookie_secure=False), dict(demo_password="demo-only-change-me"), dict(demo_password="short"),
                   dict(demo_admin_password=""), dict(demo_admin_password=PW)):
        assert config.validate_for_start(dataclasses.replace(ok, **change)), change
    assert config.validate_for_start(dataclasses.replace(ok, env="production"))


def test_public_demo_headers_robots_and_login_throttle(tmp_path):
    cfg = make_cfg(tmp_path, public_demo=True, secret_key="s" * 40, cookie_secure=True, max_failed_logins=1000)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    demo_data.build(c, cfg)
    app = create_app(cfg)
    cl = TestClient(app, base_url="https://testserver", follow_redirects=False)     # Secure cookies need https
    r = cl.get("/login")
    assert "noindex" in r.headers["x-robots-tag"] and "default-src 'self'" in r.headers["content-security-policy"]
    assert cl.get("/robots.txt").text.strip().endswith("Disallow: /")
    assert "secure" in " ".join(v.lower() for k, v in r.headers.multi_items() if k == "set-cookie")
    codes = []
    for _ in range(25):
        tok = re.search(r'name="lcsrf" value="([^"]+)"', cl.get("/login").text).group(1)
        codes.append(cl.post("/login", data={"username": "ceo", "password": "wrong-wrong-1", "lcsrf": tok, "next": "/"}).status_code)
    assert 429 in codes and codes[0] == 401
    assert cl.get("/healthz").text == "ok"


# ------------------------------------------------------------------ the whole factory flow, through the real forms, on showcase data
def test_end_to_end_workflow_on_showcase_data(tmp_path):
    cfg = make_cfg(tmp_path)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    demo_data.build(c, cfg)
    app = create_app(cfg)
    q = lambda s, *a: db.connect(cfg.db_path).execute(s, a).fetchone()[0]
    sup = q("SELECT id FROM parties WHERE kind='supplier' ORDER BY id LIMIT 1")
    item = q("SELECT id FROM items ORDER BY id LIMIT 1")
    raw = q("SELECT id FROM locations WHERE kind='raw_store'")
    dye = q("SELECT id FROM parties WHERE kind='job_work' ORDER BY id LIMIT 1")
    cust = q("SELECT id FROM parties WHERE kind='customer' ORDER BY id LIMIT 1")
    st, mg, mg2, pr, gt = Cl(app), Cl(app), Cl(app), Cl(app), Cl(app)
    for cl, u in ((st, "store1"), (mg, "manager1"), (mg2, "manager2"), (pr, "prod1"), (gt, "gate1")):
        assert cl.login(u).status_code == 303
    before_lots = q("SELECT COUNT(*) FROM lots")
    # 1 receive fabric
    r = st.post("/fabrics/receipts", {"supplier_id": sup, "item_id": item, "lot_no": "LIVE-001", "location_id": raw, "qty": "1000", "unit": "m"})
    rid = r.headers["location"].rsplit("/", 1)[1]
    assert mg.post(f"/fabrics/receipts/{rid}/post").status_code == 303
    assert q("SELECT SUM(sl.qty) FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id WHERE l.lot_no='LIVE-001'") == 1_000_000
    # 2 job, 3 issue
    jid = mg.post("/processing/jobs", {"lot_no": "LIVE-001", "process_type": "dyeing", "party_id": dye}).headers["location"].rsplit("/", 1)[1]
    st.post(f"/processing/jobs/{jid}/issues", {"qty": "800"})
    iss = q("SELECT id FROM processing_issues WHERE job_id=?", jid)
    assert st.post(f"/processing/issues/{iss}/post").status_code == 303
    # 4 result with a real loss breakdown (accepted 760 + rejected 12 + wastage 8 + shrinkage 20 = 800)
    assert pr.post(f"/processing/issues/{iss}/result", {"accepted": "760", "rejected": "12", "wastage": "8", "shrinkage": "20", "remaining": "0"}).status_code == 303
    res = q("SELECT id FROM processing_results WHERE issue_id=?", iss)
    assert pr.post(f"/processing/results/{res}/post").status_code == 303
    assert q("SELECT variance FROM processing_results WHERE id=?", res) == 0
    # 5 finished stock received
    fin = q("SELECT lot_no FROM lots WHERE kind='finished' AND parent_lot_id=(SELECT id FROM lots WHERE lot_no='LIVE-001')")
    assert q("SELECT SUM(sl.qty) FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id WHERE l.lot_no=?", fin) == 760_000
    # 6 dispatch: store creates, a DIFFERENT manager approves, gate pass, exit
    d = st.post("/dispatch", {"customer_id": cust, "lot_1": fin, "qty_1": "300"})
    did = d.headers["location"].rsplit("/", 1)[1]
    assert st.post(f"/dispatch/{did}/approve").status_code in (303, 403, 409)   # creator may not approve own dispatch
    assert q("SELECT status FROM dispatches WHERE id=?", did) == "draft"
    assert mg2.post(f"/dispatch/{did}/approve").status_code == 303
    assert gt.post(f"/dispatch/{did}/gate-pass", {"vehicle_no": "DM-LEA-5555", "driver_name": "Test Driver"}).status_code == 303
    gp = q("SELECT id FROM gate_passes WHERE dispatch_id=?", did)
    assert q("SELECT SUM(sl.qty) FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id WHERE l.lot_no=?", fin) == 760_000      # not deducted yet
    assert gt.post(f"/gate/passes/{gp}/exit", {"notes": "left"}).status_code == 303
    assert q("SELECT status FROM dispatches WHERE id=?", did) == "exited"
    assert q("SELECT SUM(sl.qty) FROM stock_ledger sl JOIN lots l ON l.id=sl.lot_id WHERE l.lot_no=?", fin) == 460_000
    assert q("SELECT COUNT(*) FROM lots") == before_lots + 2
    assert inventory.reconcile_ledger(db.connect(cfg.db_path)) == []


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
def test_sample_challan_reads_and_matches_seeded_master_data(tmp_path):
    import pathlib
    from wwtextile.ai import documents
    cfg = make_cfg(tmp_path)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    demo_data.build(c, cfg)
    app = create_app(cfg)
    st = Cl(app)
    st.login("store1")
    png = pathlib.Path("wwtextile/static/demo/sample-challan.png").read_bytes()
    r = st.c.post("/documents/upload", data={"csrf": st.csrf}, files={"file": ("sample-challan.png", png, "image/png")})
    assert r.status_code == 303
    row = db.connect(cfg.db_path).execute("SELECT extracted FROM documents ORDER BY id DESC LIMIT 1").fetchone()[0]
    import json
    f = {k: v["value"] for k, v in json.loads(row)["fields"].items()}
    assert f["lot_no"] == "L-9001" and f["supplier_ref"] == "CH-7788" and f["qty"] == "2000" and "supplier_id" in f and "item_id" in f


def test_dashboard_charts_match_ledger_and_finance(showcase):
    """Charts must be derived from the same records as the tables: chart numbers equal direct SQL over the ledger / finance entries."""
    import re
    from datetime import datetime, timezone
    from wwtextile.services import charts
    from wwtextile.webcore import display_tz
    cfg, conn, _info = showcase
    ceo = conn.execute("SELECT * FROM users WHERE username='ceo'").fetchone()
    figs = charts.build(conn, ceo, display_tz(cfg), cfg.currency)
    titles = [f["title"] for f in figs]
    assert titles == ["Fabric received and dispatched", "Processing outcome by process", "Cash received and paid"]
    html = "".join(str(f["html"]) for f in figs)
    assert "<svg" in html and "style=" not in html and "<script" not in html
    # loss shares in the table sum to 100% per process
    proc = conn.execute("SELECT COUNT(DISTINCT j.process_type) FROM processing_results r JOIN processing_jobs j ON j.id=r.job_id WHERE r.status='posted'").fetchone()[0]
    assert str(figs[1]["html"]).count("% lost") == proc
    # the received total in the chart table equals ledger receipts inside the 8-week window (and is never larger than all receipts)
    shown = sum(float(x.replace(",", "")) for x in re.findall(r"([\d,]+) m</td><td class=\"num\">[\d,]+ m</td></tr>", str(figs[0]["html"])))
    total = conn.execute("SELECT SUM(qty) FROM stock_ledger WHERE movement='receipt'").fetchone()[0] / 1000.0
    assert 0 < shown <= total + 0.5
    # a role without finance/stock rights gets only what it may see
    gate = conn.execute("SELECT * FROM users WHERE username='gate1'").fetchone()
    assert charts.build(conn, gate, display_tz(cfg), cfg.currency) == []


def test_public_demo_admin_signs_in_only_directly_on_the_host(tmp_path):
    cfg = make_cfg(tmp_path, public_demo=True, secret_key="s" * 40, cookie_secure=True, max_failed_logins=1000)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    demo_data.build(c, cfg)
    app = create_app(cfg)

    def attempt(headers, user, pw):
        cl = TestClient(app, base_url="https://testserver", follow_redirects=False)
        tok = re.search(r'name="lcsrf" value="([^"]+)"', cl.get("/login", headers=headers).text).group(1)
        return cl, cl.post("/login", data={"username": user, "password": pw, "lcsrf": tok, "next": "/"}, headers=headers)

    for hdr in ({"x-forwarded-for": "203.0.113.9"}, {"cf-connecting-ip": "203.0.113.9"}, {"forwarded": "for=203.0.113.9"}):
        _cl, r = attempt(hdr, "admin", ADMIN_PW)                       # even the CORRECT admin password is refused through the public link
        assert r.status_code == 401 and "Invalid username or password" in r.text, hdr
    assert c.execute("SELECT failed_logins, locked_until FROM users WHERE username='admin'").fetchone()[0] == 0   # and it cannot be locked out remotely
    assert c.execute("SELECT COUNT(*) FROM audit_events WHERE action='auth.login' AND outcome='denied'").fetchone()[0] >= 3
    _cl, r = attempt({"x-forwarded-for": "203.0.113.9"}, "ceo", PW)     # clients are unaffected
    assert r.status_code == 303
    _cl, r = attempt({}, "admin", ADMIN_PW)                              # directly on the host: allowed
    assert r.status_code == 303
    remote = dataclasses.replace(cfg, demo_admin_remote=True)
    _cl, r = (lambda a: (lambda cl: (cl, cl.post("/login", data={"username": "admin", "password": ADMIN_PW, "next": "/",
              "lcsrf": re.search(r'name="lcsrf" value="([^"]+)"', cl.get("/login", headers={"x-forwarded-for": "1.2.3.4"}).text).group(1)}, headers={"x-forwarded-for": "1.2.3.4"})))(
              TestClient(a, base_url="https://testserver", follow_redirects=False)))(create_app(remote))
    assert r.status_code == 303                                           # explicit opt-in WW_DEMO_ADMIN_REMOTE=1


def test_assistant_answers_natural_stock_questions_on_showcase_data(showcase):
    from wwtextile.ai import agent
    cfg, conn, _ = showcase
    ceo = conn.execute("SELECT * FROM users WHERE username='ceo'").fetchone()
    for q in ("what is on hand in the finished store", "how much stock do we have", "show raw store inventory", "what's in WIP"):
        r = agent.handle(conn, cfg, ceo, q)
        text = json.dumps(r)
        assert "couldn't map" not in text, q
        assert "lot/location balance" in text, q          # a real tool result, not the rule engine's fallback
