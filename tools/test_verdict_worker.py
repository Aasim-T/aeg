import unittest
from unittest.mock import patch

from risk_model import RiskModel
from verdict_worker import Anomaly, POST_VERDICT, handle, unpack_signature


def word(value):
    return f"{value:064x}"


class WorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = RiskModel()

    def test_neural_model_produces_all_three_recommendations(self):
        self.assertEqual(self.model.recommend(40, 0, 0), 0)
        self.assertEqual(self.model.recommend(20, 0, 20), 1)
        self.assertEqual(self.model.recommend(0, 40, 20), 2)
        self.assertEqual(self.model.recommend(80, 0, 0), 0)

    def test_anomaly_relays_model_tier_and_signature(self):
        anomaly_id = "0x" + word(99)
        log = {
            "topics": ["0x" + word(1), anomaly_id, "0x" + word(2)],
            "data": "0x" + "".join(map(word, (60, 0, 40, 20, 1120))),
        }
        digest = "0x" + word(4)
        signature = "0x" + word(10) + word(11) + "01"
        tx = "0x" + word(5)
        with patch("verdict_worker.cast", side_effect=["false", digest, signature, tx]) as cast:
            with patch("verdict_worker.rpc", side_effect=[{"timestamp": hex(1000)}, {"status": "0x1"}]):
                handle(Anomaly.from_log(log), self.model, "base", "registry", "signer", "relayer")

        cast.assert_any_call("wallet", "sign", "--no-hash", "--account", "signer", digest)
        cast.assert_any_call(
            "send", "registry", POST_VERDICT, anomaly_id, 2, 2, 1120,
            "0x" + word(10), "0x" + word(11), 28, "--account", "relayer", "--rpc-url", "base", "--async",
        )

    def test_invalid_and_expiring_payloads_are_skipped(self):
        with self.assertRaises(ValueError):
            unpack_signature("0xdead")
        expired = Anomaly("0x" + word(99), 1, 0, 40, 20, 1005)
        with patch("verdict_worker.cast") as cast:
            with patch("verdict_worker.rpc", return_value={"timestamp": hex(1000)}):
                handle(expired, self.model, "base", "registry", "signer", "relayer")
        cast.assert_not_called()
