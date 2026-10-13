import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg
import pytest

import pull_ebay_compat as pull
from pull_ebay_compat import RateLimited, Result

NSDECL = 'xmlns="urn:ebay:apis:eBLBaseComponents"'


def reply(ack="Success", body=""):
    return f'<?xml version="1.0"?><GetItemResponse {NSDECL}><Ack>{ack}</Ack>{body}</GetItemResponse>'


def compat(*pairs, notes=None):
    nv = "".join(f"<NameValueList><Name>{n}</Name><Value>{v}</Value></NameValueList>" for n, v in pairs)
    n = f"<CompatibilityNotes>{notes}</CompatibilityNotes>" if notes else ""
    return f"<Compatibility>{nv}{n}</Compatibility>"


def failure(code, msg="boom", severity="Error"):
    return reply("Failure", f"<Errors><ShortMessage>x</ShortMessage><LongMessage>{msg}</LongMessage>"
                            f"<ErrorCode>{code}</ErrorCode><SeverityCode>{severity}</SeverityCode></Errors>")


# ----------------------------------------------------------------- parsing
def test_parse_list_keeps_everything_as_sent():
    xml = reply(body="<Item><ItemCompatibilityCount>2</ItemCompatibilityCount><ItemCompatibilityList>"
                + compat(("Year", "2019"), ("Make", "Honda"), ("Model", "CR125R"), ("Trim", "Base"),
                         notes="Relation Per Car Quantity:1&lt;br&gt;")
                + compat(("Year", "2020"), ("Make", "Honda"), ("Model", "CR125R"), ("Engine", "A"), ("Engine", "B"))
                + "</ItemCompatibilityList></Item>")
    r = pull.parse_get_item(xml)
    assert r.status == "ok" and r.count == 2
    assert r.compat[0] == {"attrs": {"Year": "2019", "Make": "Honda", "Model": "CR125R", "Trim": "Base"},
                           "notes": "Relation Per Car Quantity:1<br>"}
    assert r.compat[1]["attrs"]["Engine"] == ["A", "B"]          # repeated field kept, not dropped
    assert r.compat[1]["notes"] is None


def test_parse_tolerates_unescaped_br_in_notes():
    """eBay's docs show a bare <br> in CompatibilityNotes; that is not well-formed XML."""
    xml = reply(body="<Item><ItemCompatibilityList><Compatibility><NameValueList><Name>Year</Name><Value>2019</Value>"
                     "</NameValueList><CompatibilityNotes>Relation Per Car Quantity:1<br></CompatibilityNotes>"
                     "</Compatibility></ItemCompatibilityList></Item>")
    r = pull.parse_get_item(xml)
    assert r.status == "ok" and r.compat[0]["notes"] == "Relation Per Car Quantity:1<br>"


def test_parse_warning_ack_still_reads_list():
    xml = reply("Warning", "<Item><ItemCompatibilityList>" + compat(("Year", "2001")) + "</ItemCompatibilityList></Item>")
    assert pull.parse_get_item(xml).status == "ok"


def test_parse_no_list():
    r = pull.parse_get_item(reply(body="<Item><ItemID>1</ItemID></Item>"))
    assert (r.status, r.count) == ("no_list", 0)


def test_parse_count_only_means_eBay_has_compat_but_did_not_send_it():
    r = pull.parse_get_item(reply(body="<Item><ItemCompatibilityCount>140</ItemCompatibilityCount></Item>"))
    assert (r.status, r.count, r.compat) == ("count_only", 140, None)


def test_parse_ended_listing():
    assert pull.parse_get_item(failure("17", "cannot be accessed")).status == "not_found"


def test_parse_other_failure_keeps_code_and_message():
    r = pull.parse_get_item(failure("931", "Auth token is invalid"))
    assert (r.status, r.code, r.message) == ("error", "931", "Auth token is invalid")


def test_parse_rate_limit_raises():
    with pytest.raises(RateLimited):
        pull.parse_get_item(failure("518", "Call usage limit has been reached."))


def test_parse_warning_severity_errors_are_not_failures():
    xml = reply("Failure", "<Errors><LongMessage>just a warning</LongMessage><ErrorCode>5</ErrorCode>"
                           "<SeverityCode>Warning</SeverityCode></Errors>")
    r = pull.parse_get_item(xml)
    assert r.status == "error"                                   # Ack says failure; reported, not crashed


def test_parse_garbage():
    assert pull.parse_get_item("<html>not xml").status == "error"
    assert pull.parse_get_item(reply(ack="Weird")).status == "error"


# ----------------------------------------------------------------- building requests
def test_request_body_and_headers_for_authnauth_token():
    c = pull.EbayTrading(auth_token="tok&<en>", post=lambda *a: None)
    body = c.body("123456789012")
    assert "<eBayAuthToken>tok&amp;&lt;en&gt;</eBayAuthToken>" in body          # escaped, still valid XML
    assert "<ItemID>123456789012</ItemID>" in body
    assert "<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>" in body
    h = c.headers()
    assert h["X-EBAY-API-CALL-NAME"] == "GetItem" and h["X-EBAY-API-SITEID"] == "0"
    assert "X-EBAY-API-IAF-TOKEN" not in h and "X-EBAY-API-APP-NAME" not in h
    import xml.etree.ElementTree as ET
    ET.fromstring(body)                                                       # well-formed


def test_oauth_token_goes_in_header_not_body():
    c = pull.EbayTrading(oauth_token="v^1.1#abc", post=lambda *a: None)
    assert c.headers()["X-EBAY-API-IAF-TOKEN"] == "v^1.1#abc"
    assert "eBayAuthToken" not in c.body("1")


def test_app_dev_cert_headers_only_when_all_three_given():
    assert "X-EBAY-API-APP-NAME" not in pull.EbayTrading(auth_token="t", app_id="a", dev_id="d", post=None).headers()
    h = pull.EbayTrading(auth_token="t", app_id="a", dev_id="d", cert_id="c", post=None).headers()
    assert (h["X-EBAY-API-APP-NAME"], h["X-EBAY-API-DEV-NAME"], h["X-EBAY-API-CERT-NAME"]) == ("a", "d", "c")


def test_client_needs_a_token(monkeypatch):
    with pytest.raises(SystemExit):
        pull.client_from_env({})
    with pytest.raises(ValueError):
        pull.EbayTrading()


# ----------------------------------------------------------------- retries
def test_server_errors_are_retried_then_reported():
    sleeps, calls = [], []

    def post(url, h, b):
        calls.append(1)
        return 503, "busy"

    r = pull.EbayTrading(auth_token="t", post=post, sleep=sleeps.append).get_item_compat("1")
    assert r.status == "error" and "503" in r.message
    assert len(calls) == 3 and sleeps == [1, 2]                    # backoff, no sleep after the last try


def test_transient_failure_then_success():
    seq = iter([(500, ""), (200, reply(body="<Item><ItemCompatibilityList>" + compat(("Year", "1")) + "</ItemCompatibilityList></Item>"))])
    r = pull.EbayTrading(auth_token="t", post=lambda *a: next(seq), sleep=lambda s: None).get_item_compat("1")
    assert r.status == "ok"


def test_network_exception_is_retried():
    n = {"i": 0}

    def post(*a):
        n["i"] += 1
        raise urllib.error.URLError("dns down")

    r = pull.EbayTrading(auth_token="t", post=post, sleep=lambda s: None).get_item_compat("1")
    assert r.status == "error" and "network" in r.message and n["i"] == 3


def test_real_http_roundtrip_against_local_server():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
            out = reply(body="<Item><ItemCompatibilityCount>1</ItemCompatibilityCount><ItemCompatibilityList>"
                        + compat(("Year", "2019"), ("Make", "Yamaha"), ("Model", "YZ450F")) + "</ItemCompatibilityList></Item>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = pull.EbayTrading(auth_token="secret-token", endpoint=f"http://127.0.0.1:{srv.server_port}/ws/api.dll")
        r = c.get_item_compat("555000111222")
    finally:
        srv.shutdown()
    assert r.status == "ok" and r.compat[0]["attrs"]["Make"] == "Yamaha"
    assert seen["headers"]["x-ebay-api-call-name"] == "GetItem"
    assert seen["headers"]["content-type"] == "text/xml"
    assert "555000111222" in seen["body"] and "secret-token" in seen["body"]


# ----------------------------------------------------------------- database runs
class FakeEbay:
    def __init__(self, replies=None, default=None):
        self.replies, self.default, self.calls = replies or {}, default, []

    def get_item_compat(self, item_id):
        self.calls.append(item_id)
        r = self.replies.get(item_id, self.default or Result("no_list", count=0))
        if isinstance(r, Exception):
            raise r
        return r


def ok(*rows):
    return Result("ok", count=len(rows), compat=[{"attrs": dict(r), "notes": None} for r in rows])


@pytest.fixture
def db(db_url):
    def wipe():
        with psycopg.connect(db_url, autocommit=True) as c:
            c.execute("truncate products, ebay_compat_raw, channel_listings restart identity cascade")
    wipe()
    c = psycopg.connect(db_url)
    yield c
    c.close()
    wipe()


def seed(conn, item_ids, channel="eBay"):
    """One product + one listing per id on the named channel."""
    cur = conn.cursor()
    cur.execute("select id from sales_channels where name = %s", (channel,))
    ch = cur.fetchone()[0]
    for i, item in enumerate(item_ids):
        cur.execute("insert into products (sku, slug, title) values (%s,%s,%s) returning id",
                    (f"{channel}-{i}-{item}", f"{channel}-{i}-{item}".lower().replace(" ", "-"), f"Part {item}"))
        pid = cur.fetchone()[0]
        cur.execute("insert into channel_listings (product_id, channel_id, external_id, status) "
                    "values (%s,%s,%s,'active')", (pid, ch, item))
    conn.commit()


def rows(db_url):
    with psycopg.connect(db_url) as c:
        return c.execute("select ebay_item_id, status from ebay_compat_raw order by ebay_item_id").fetchall()


def test_sample_mode_saves_nothing_and_respects_sample_size(db, db_url):
    seed(db, [str(1000 + i) for i in range(10)])
    fake = FakeEbay(default=ok({"Make": "Honda"}))
    rep = pull.run_pull(db, fake, commit=False, sample=4, sleep=lambda s: None)
    assert len(fake.calls) == 4 and rep.calls == 4
    assert rows(db_url) == []
    assert "Still to fetch: 10" in rep.render()


def test_commit_saves_and_is_resumable(db, db_url):
    ids = [str(2000 + i) for i in range(5)]
    seed(db, ids)
    fake = FakeEbay(default=ok({"Make": "Honda"}))
    first = pull.run_pull(db, fake, commit=True, max_calls=2, sleep=lambda s: None)
    assert len(rows(db_url)) == 2 and "3 listings remain" in first.stopped
    second = pull.run_pull(db, fake, commit=True, max_calls=100, sleep=lambda s: None)
    assert len(rows(db_url)) == 5 and second.stopped is None
    assert sorted(fake.calls) == sorted(ids)                       # each listing fetched exactly once
    third = pull.run_pull(db, fake, commit=True, sleep=lambda s: None)
    assert third.calls == 0 and len(fake.calls) == 5               # nothing left; no wasted calls


def test_errors_are_stored_and_retried_next_time_but_successes_are_not(db, db_url):
    seed(db, ["3001", "3002", "3003"])
    flaky = FakeEbay({"3002": Result("error", code="10", message="temporary")}, default=ok({"Make": "Kawasaki"}))
    pull.run_pull(db, flaky, commit=True, sleep=lambda s: None)
    assert dict(rows(db_url))["3002"] == "error"
    again = FakeEbay(default=ok({"Make": "Kawasaki"}))
    pull.run_pull(db, again, commit=True, sleep=lambda s: None)
    assert again.calls == ["3002"]                                  # only the failed one
    assert dict(rows(db_url))["3002"] == "ok"


def test_rate_limit_stops_cleanly_and_keeps_progress(db, db_url):
    seed(db, ["4001", "4002", "4003", "4004"])
    fake = FakeEbay({"4003": RateLimited()}, default=ok({"Make": "Suzuki"}))
    rep = pull.run_pull(db, fake, commit=True, sleep=lambda s: None)
    assert "518" in rep.stopped and "tomorrow" in rep.stopped
    assert [i for i, _ in rows(db_url)] == ["4001", "4002"]         # saved before the limit hit
    assert fake.calls == ["4001", "4002", "4003"]                   # did not keep hammering eBay
    resumed = pull.run_pull(db, FakeEbay(default=ok({"Make": "Suzuki"})), commit=True, sleep=lambda s: None)
    assert len(rows(db_url)) == 4 and resumed.stopped is None


def test_three_errors_in_a_row_aborts_with_token_hint(db, db_url):
    seed(db, [str(5000 + i) for i in range(10)])
    bad = FakeEbay(default=Result("error", code="931", message="Auth token is invalid"))
    rep = pull.run_pull(db, bad, commit=True, sleep=lambda s: None)
    assert len(bad.calls) == 3 and "token" in rep.stopped.lower()


def test_a_success_resets_the_error_streak(db, db_url):
    seed(db, [str(6000 + i) for i in range(6)])
    err = Result("error", message="x")
    fake = FakeEbay({"6000": err, "6001": err, "6003": err, "6004": err}, default=ok({"Make": "KTM"}))
    rep = pull.run_pull(db, fake, commit=True, sleep=lambda s: None)
    assert rep.stopped is None and len(fake.calls) == 6


def test_only_numeric_ebay_item_numbers_are_fetched(db, db_url):
    seed(db, ["7001", "EBAY-JUNK"])
    seed(db, ["7002"], channel="In Person")                         # another channel: never sent to eBay
    fake = FakeEbay(default=ok({"Make": "Polaris"}))
    rep = pull.run_pull(db, fake, commit=True, sleep=lambda s: None)
    assert fake.calls == ["7001"]
    assert "Skipped (no usable item number): 1" in rep.render()


def test_raw_json_is_stored_untouched_and_queryable(db, db_url):
    seed(db, ["8001"])
    fake = FakeEbay({"8001": Result("ok", count=1, compat=[
        {"attrs": {"Year": "2019", "Make": "Yamaha", "Model": "YZ450F", "Engine": ["A", "B"]}, "notes": "fits <b>all</b>"}])})
    pull.run_pull(db, fake, commit=True, sleep=lambda s: None)
    with psycopg.connect(db_url) as c:
        make, notes, engines, cnt = c.execute(
            "select compat_list->0->'attrs'->>'Make', compat_list->0->>'notes', "
            "compat_list->0->'attrs'->'Engine', compat_count from ebay_compat_raw").fetchone()
    assert (make, notes, engines, cnt) == ("Yamaha", "fits <b>all</b>", ["A", "B"], 1)


def test_no_vehicle_or_fitment_tables_are_touched(db, db_url):
    seed(db, ["9001"])
    pull.run_pull(db, FakeEbay(default=ok({"Make": "Honda", "Model": "CR125R", "Year": "2000"})),
                  commit=True, sleep=lambda s: None)
    with psycopg.connect(db_url) as c:
        for t in ("vehicles", "vehicle_makes", "vehicle_models", "product_fitment"):
            assert c.execute(f"select count(*) from {t}").fetchone()[0] == 0


def test_report_profiles_what_ebay_sent(db, db_url):
    seed(db, ["1101", "1102"])
    fake = FakeEbay({"1101": ok({"Year": "2000", "Make": "HONDA", "Model": "CR125R"}, {"Year": "2001", "Make": "Honda", "Model": "CR125R"}),
                     "1102": Result("count_only", count=40)})
    text = pull.run_pull(db, fake, commit=False, sample=10, sleep=lambda s: None).render()
    assert "Honda (2)" in text and "Year (2)" in text and "count_only" in text and "SAMPLE" not in text


def test_max_calls_limits_a_run(db, db_url):
    seed(db, [str(1200 + i) for i in range(6)])
    fake = FakeEbay(default=ok({"Make": "Honda"}))
    pull.run_pull(db, fake, commit=True, max_calls=3, sleep=lambda s: None)
    assert len(fake.calls) == 3


def test_status_constraint_rejects_unknown_values(db, db_url):
    seed(db, ["1301"])
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into ebay_compat_raw (channel_listing_id, ebay_item_id, status) "
                   "select id, '1301', 'bogus' from channel_listings")


def test_cli_single_item_mode_needs_no_database(monkeypatch, capsys):
    monkeypatch.setattr(pull, "client_from_env", lambda: FakeEbay({"123": ok({"Year": "2019", "Make": "Honda"})}))
    pull.main(["--item", "123"])
    assert "Honda" in capsys.readouterr().out


def test_cli_single_item_error_exits_nonzero(monkeypatch):
    monkeypatch.setattr(pull, "client_from_env", lambda: FakeEbay({"123": Result("error", code="931", message="bad token")}))
    with pytest.raises(SystemExit) as e:
        pull.main(["--item", "123"])
    assert "bad token" in str(e.value)


def test_cli_full_run_needs_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(pull, "client_from_env", lambda: FakeEbay())
    with pytest.raises(SystemExit) as e:
        pull.main([])
    assert "DATABASE_URL" in str(e.value)
