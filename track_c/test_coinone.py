import base64
from decimal import Decimal
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import uuid

from .coinone import CoinoneError, CoinoneReadOnly, Credentials, NoRedirect, decimal, exchange
from .preflight import round_trip


class AuthenticationTests(unittest.TestCase):
    def test_read_requests_sign_the_exact_payload_with_unique_nonces(self):
        seen = []
        def transport(req, timeout):
            seen.append(req)
            return {"balances": []}
        client = CoinoneReadOnly(Credentials("test-access", "test-secret"), transport=transport)
        client.balances()
        client.balances()
        nonces = []
        for request in seen:
            headers = {k.lower(): v for k, v in request.header_items()}
            encoded = headers["x-coinone-payload"].encode("ascii")
            self.assertEqual(base64.b64decode(encoded), request.data)
            self.assertEqual(hmac.new(b"test-secret", encoded, hashlib.sha512).hexdigest(), headers["x-coinone-signature"])
            data = json.loads(base64.b64decode(encoded))
            self.assertEqual(data["access_token"], "test-access")
            self.assertEqual(uuid.UUID(data["nonce"]).version, 4)
            nonces.append(data["nonce"])
        self.assertEqual(len(set(nonces)), 2)

    def test_write_endpoints_and_path_injection_are_rejected_before_transport(self):
        with patch("track_c.coinone.exchange") as transport:
            client = CoinoneReadOnly(Credentials("test-access", "test-secret"), transport=transport)
            for path in ("/v2.1/order", "/v2.1/order/cancel", "/v2.1/transaction/coin/withdrawal", "https://example.com", "/v2.1/account/trade_fee/KRW/BTC?leak=yes"):
                with self.assertRaises(CoinoneError):
                    client._post(path)
            with self.assertRaises(ValueError):
                client.market("BTC/../../order")
            transport.assert_not_called()

    def test_redirect_does_not_forward_authentication(self):
        with self.assertRaisesRegex(CoinoneError, "redirect refused"):
            NoRedirect().redirect_request(None, None, 302, "test-secret", None, "https://example.com")

    def test_errors_do_not_echo_remote_messages_or_payloads(self):
        bad = json.dumps({"result": "error", "error_code": "test-access", "error_msg": "test-secret"}).encode()
        response = io.BytesIO(bad)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(CoinoneError, "Coinone API error unknown") as caught:
                exchange(None, 1)
        self.assertNotIn("test-secret", str(caught.exception))
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError("https://example.com/test-access", 403, "test-secret", {}, None)
            with self.assertRaisesRegex(CoinoneError, "^HTTP 403$"):
                exchange(None, 1)

    def test_credentials_read_bom_quotes_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            raw = 'COINONE_ACCESS_TOKEN="test-access"\nCOINONE_SECRET_KEY=\'test-secret\'\nBITGET_SECRET_KEY=unrelated\n'
            path.write_text(raw, encoding="utf-8-sig")
            before = path.read_bytes()
            creds = Credentials.read(path, {})
            self.assertEqual(creds.access_token, "test-access")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(repr(creds), "Credentials()")
            with self.assertRaises(CoinoneError):
                Credentials.read(path, {"COINONE_ACCESS_TOKEN": "another-account"})

    def test_conflicting_aliases_do_not_guess_the_account(self):
        with self.assertRaisesRegex(CoinoneError, "conflicting credential aliases"):
            Credentials.read("missing.env", {"COINONE_ACCESS_TOKEN": "a", "COINONE_API_KEY": "b", "COINONE_SECRET_KEY": "s"})

    def test_aws_profile_selects_the_named_pair_without_mixing_default(self):
        env = {"coinone-api-key-aws": "a", "coinone-secret-key-aws": "s", "COINONE_ACCESS_TOKEN": "different"}
        self.assertEqual(Credentials.read("missing.env", env, profile="aws"), Credentials("a", "s"))
        with self.assertRaises(CoinoneError):
            Credentials.read("missing.env", {"COINONE_ACCESS_TOKEN": "a", "COINONE_SECRET_KEY": "s"}, profile="aws")

    def test_fee_response_must_match_requested_pair(self):
        client = CoinoneReadOnly(Credentials("a", "s"), transport=lambda r, t: {"fee_rates": [{"quote_currency": "KRW", "target_currency": "ETH", "maker": "0", "taker": "0"}]})
        with self.assertRaises(CoinoneError):
            client.fees("BTC")


class DepthTests(unittest.TestCase):
    def setUp(self):
        self.book = dict(asks=[dict(price="101", qty="1"), dict(price="102", qty="10")], bids=[dict(price="100", qty="2"), dict(price="99", qty="10")])
        self.market = dict(qty_unit="0.1", min_qty="0.1", min_order_amount="10")
        self.units = [dict(range_min="0", price_unit="1")]

    def test_full_depth_and_actual_fee_instead_of_assuming_free(self):
        result = round_trip(self.book, self.market, self.units, Decimal("252.5"), dict(taker="0.001"))
        self.assertEqual(result["probe_qty"], "2.5")
        self.assertEqual(Decimal(result["buy_notional_krw"]), Decimal("254"))
        self.assertEqual(Decimal(result["sell_notional_krw"]), Decimal("249.5"))
        self.assertEqual(Decimal(result["account_fee_inclusive_bps"]), ((Decimal("254.254") - Decimal("249.2505")) / Decimal("254") * 10000))

    def test_insufficient_depth_is_not_a_cheap_fill(self):
        result = round_trip(self.book, self.market, self.units, Decimal("1000000"), dict(taker="0"))
        self.assertEqual(result["depth_status"], "insufficient_visible_depth")
        self.assertNotIn("account_fee_inclusive_bps", result)

    def test_unknown_fee_stays_unknown(self):
        result = round_trip(self.book, self.market, self.units, Decimal("100"), None)
        self.assertIsNone(result["account_fee_inclusive_bps"])

    def test_subminimum_and_nonfinite_amount_are_rejected(self):
        self.assertEqual(round_trip(self.book, self.market, self.units, Decimal("1"), None)["depth_status"], "below_minimum_order")
        for number in ("NaN", "Infinity", "-1", "0"):
            with self.assertRaises(CoinoneError):
                decimal(number, positive=True)


if __name__ == "__main__":
    unittest.main()
