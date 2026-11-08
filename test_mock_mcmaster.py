#
# partcad-svc-commerce-mcmaster, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""The plugin against mock_mcmaster.py: real HTTPS, a real client certificate, McMaster-Carr's protocol.

test_mcmaster.py answers the plugin from a table; this runs the mock server
on a free port, with a certificate authority, a server certificate and a
PKCS#12 client certificate made for the test, and has the plugin log in,
subscribe, price and download over TLS the way it would at McMaster-Carr -
which is where its certificate handling is tested at all. It needs
'cryptography', as the plugin does for a PKCS#12 certificate.
"""

import base64
import os
import runpy
import socket
import ssl
import urllib.error
import urllib.request

import pytest

import distributor as d

pytest.importorskip("cryptography")

_HERE = os.path.dirname(os.path.abspath(__file__))
plugin = runpy.run_path(os.path.join(_HERE, "mcmaster.py"), run_name="mcmaster_under_test")
McMaster = plugin["McMaster"]
mock = runpy.run_path(os.path.join(_HERE, "mock_mcmaster.py"), run_name="mock_mcmaster_under_test")


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    return mock["make_certificates"](str(tmp_path_factory.mktemp("certs")))


@pytest.fixture
def server(certs):
    made = mock["MockServer"](certs, port=0).start()
    yield made
    made.shutdown()
    made.server_close()


@pytest.fixture
def mcmaster(server, tmp_path, monkeypatch):
    monkeypatch.setenv("PARTCAD_MCMASTER_CACHE", str(tmp_path / "cache"))
    for variable in McMaster.CREDENTIALS.values():
        monkeypatch.delenv(variable, raising=False)

    def make(**overrides):
        parameters = dict(server.parameters(), **overrides)
        return McMaster({"parameters": parameters, "userInfo": {"country": "US", "currency": "USD"}})

    return make


def test_a_part_comes_over_tls_with_the_client_certificate(mcmaster, server):
    made = mcmaster()
    config = made.get("objects/part/91290A115")
    assert config["type"] == "step" and config["sku"] == "91290A115" and config["count_per_sku"] == 100
    step = base64.b64decode(made.get("files/" + config["path"]))
    assert step.startswith(b"ISO-10303-21;")
    assert server.catalog.log[:3] == [
        ("POST", "/v1/login"),
        ("PUT", "/v1/products"),
        ("GET", "/v1/products/91290A115/price"),
    ]
    assert server.catalog.subscribed == ["91290A115"]


def test_a_pack_said_only_in_the_specifications_is_read(mcmaster):
    assert mcmaster().get("objects/part/94180A331")["count_per_sku"] == 50


def test_a_quote_is_priced_at_the_break_the_order_reaches(mcmaster):
    cart = {"parts": {"bearing": {"vendor": "mcmaster", "sku": "6661K14", "count": 12}}}
    answer = mcmaster().quote({"cart": cart})
    assert answer["currency"] == "USD" and answer["price"] == pytest.approx(12 * 5.41)


def test_the_token_is_kept_and_not_asked_for_again(mcmaster, server):
    mcmaster().get("objects/part/91290A115")
    mcmaster().avail({"vendor": "mcmaster", "sku": "6661K14", "count": 1})
    assert [entry for entry in server.catalog.log if entry[1] == "/v1/login"] == [("POST", "/v1/login")]


def test_an_expired_token_is_replaced(mcmaster, server):
    made = mcmaster()
    made.get("objects/part/91290A115")
    server.catalog.tokens.clear()  # McMaster-Carr forgot it: 403 EXPIRED_AUTHORIZATION_TOKEN
    assert made.avail({"vendor": "mcmaster", "sku": "6661K14", "count": 1})["available"] is True
    assert [entry for entry in server.catalog.log if entry[1] == "/v1/login"] == [("POST", "/v1/login")] * 2


def test_a_part_mcmaster_does_not_have_is_said(mcmaster):
    assert mcmaster().avail({"vendor": "mcmaster", "sku": "12345A678", "count": 1}) == {
        "available": False,
        "reason": "McMaster-Carr has no part 12345A678",
    }


def test_a_discontinued_part_is_not_available(mcmaster):
    answer = mcmaster().avail({"vendor": "mcmaster", "sku": "4936K451", "count": 1})
    assert answer == {"available": False, "reason": "4936K451 is discontinued at McMaster-Carr"}


def test_a_wrong_password_is_refused_with_what_mcmaster_said(mcmaster):
    with pytest.raises(d.NeedsCredentials, match="LOGIN_FAILED: Incorrect username or password"):
        mcmaster(password="wrong").fetch_product("91290A115")


def test_a_wrong_certificate_password_says_so(mcmaster):
    with pytest.raises(d.NeedsCredentials, match="the certificate could not be opened"):
        mcmaster(certificate_password="wrong").fetch_product("91290A115")


def test_the_certificate_can_be_given_as_its_content(mcmaster, certs):
    with open(certs["client"], "rb") as f:
        content = base64.b64encode(f.read()).decode("ascii")
    assert mcmaster(certificate=content).get("objects/part/6661K14")["type"] == "step"


def test_without_a_client_certificate_the_server_refuses_the_handshake(server, certs):
    context = ssl.create_default_context(cafile=certs["ca"])
    with pytest.raises((ssl.SSLError, urllib.error.URLError, ConnectionResetError, socket.error)):
        with urllib.request.urlopen(urllib.request.Request(server.url + "/v1/login", data=b"{}"), context=context) as r:
            r.read()


def test_a_server_the_ca_does_not_vouch_for_is_not_trusted(mcmaster, capsys):
    made = mcmaster(ca="")
    assert d.pm.serve(made, "objects/part/91290A115") is None
    assert "CERTIFICATE_VERIFY_FAILED" in capsys.readouterr().err


def test_logging_in_checks_the_account_with_the_server(mcmaster, server):
    made = mcmaster()
    assert made.whoami(dict(made.keys)) == mock["USERNAME"]
    assert made.whoami(dict(made.keys, password="wrong")) is None


def test_the_subscription_limit_is_said(mcmaster, server):
    server.catalog.max_per_day = 1
    made = mcmaster()
    made.get("objects/part/91290A115")
    answer = made.avail({"vendor": "mcmaster", "sku": "6661K14", "count": 1})
    assert answer["available"] is False


def test_the_server_answers_what_mcmaster_does_without_a_subscription(server, certs):
    """The mock itself: nothing is read before it is subscribed to."""
    catalog = server.catalog
    status, token = catalog.login({"UserName": mock["USERNAME"], "Password": mock["PASSWORD"]})
    headers = {"Authorization": "Bearer " + token["AuthToken"]}
    assert catalog.answer("GET", "/v1/products/6661K14", headers, None)[1]["ErrorMessage"] == (
        "NOT_SUBSCRIBED_TO_PRODUCT"
    )
    assert catalog.answer("PUT", "/v1/products", headers, {"URL": "https://mcmaster.com/6661K14"})[0] == 201
    assert catalog.answer("PUT", "/v1/products", headers, {"URL": "https://mcmaster.com/6661K14"})[0] == 200
    assert catalog.answer("GET", "/v1/products/6661K14", headers, None)[1]["PartNumber"] == "6661K14"
    assert catalog.answer("DELETE", "/v1/products", headers, {"URL": "https://mcmaster.com/6661K14"})[0] == 204
    assert catalog.answer("GET", "/v1/products/6661K14", headers, None)[0] == 403
