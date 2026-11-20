import unittest
from unittest.mock import patch

from verify_emergency import EMERGENCY, PROTECTION_FAILED, TIER_CHANGED, TRANSFER, abi_word, verify


def addr(value):
    return f"0x{value:040x}"


class EmergencyProofTest(unittest.TestCase):
    def setUp(self):
        self.origin, self.reaction, self.callback = map(lambda n: abi_word(hex(n)), (11, 12, 13))
        self.epoch = abi_word("0x1")
        self.vault, self.asset, self.reactive, self.reserve, self.operator = map(addr, range(1, 6))
        self.signatures = {
            name: abi_word(hex(index)) for index, name in enumerate(
                (TRANSFER, EMERGENCY, TIER_CHANGED, PROTECTION_FAILED), 20
            )
        }

    def run_proof(self, *, debt=0, failed=False, match=True):
        origin_receipt = {"status": "0x1", "blockNumber": "0x10", "logs": [{
            "address": self.asset,
            "topics": [self.signatures[TRANSFER], abi_word(self.vault), abi_word(self.operator)],
            "data": abi_word("0x20"),
        }]}
        reaction = {
            "topics": [self.signatures[EMERGENCY], self.epoch, self.origin],
            "transactionHash": self.reaction, "blockNumber": "0x20", "data": abi_word("0x50"),
        }
        callback = {
            "address": self.vault,
            "topics": [self.signatures[TIER_CHANGED], self.epoch, self.origin],
            "transactionHash": self.callback, "blockNumber": "0x14", "data": abi_word("0x3"),
        }
        callback_receipt = {"status": "0x1", "logs": [callback]}
        if failed:
            callback_receipt["logs"].append({
                "address": self.vault, "topics": [self.signatures[PROTECTION_FAILED]],
            })

        def chain_rpc(url, method, params=()):
            if method == "eth_getTransactionReceipt":
                return {self.origin: origin_receipt, self.reaction: {"status": "0x1"},
                        self.callback: callback_receipt}[params[0]]
            if method == "eth_blockNumber":
                return "0x20" if url == "lasna" else "0x14"
            if method == "eth_getLogs":
                expected = [self.signatures[EMERGENCY], None, self.origin] if url == "lasna" else callback["topics"]
                self.assertEqual(params[0]["topics"], expected)
                self.assertEqual(params[0]["address"], self.reactive if url == "lasna" else self.vault)
                return [reaction] if url == "lasna" else [callback] if match else []
            if method == "eth_getBlockByNumber":
                return {"timestamp": {"0x10": "0x100", "0x20": "0x101", "0x14": "0x102"}[params[0]]}
            self.fail(f"Unexpected RPC call {method}")

        def contract_read(url, target, signature, *args):
            return str({
                "debt(address)(uint256)": debt if url == "base" else 0,
                "balanceOf(address)(uint256)": 0,
                "tier()(uint8)": 3,
                "epoch()(uint64)": 1,
            }[signature])

        manifest = {
            "contracts": {"asset": self.asset, "vault": self.vault, "reactive": self.reactive},
            "settings": {"roles": {"reserve": self.reserve}},
        }
        with (
            patch("verify_emergency.rpc", side_effect=chain_rpc),
            patch("verify_emergency.cast", side_effect=lambda _, name: self.signatures[name]),
            patch("verify_emergency.call", side_effect=contract_read),
        ):
            return verify("base", "lasna", manifest, self.origin, 30, 0)

    def test_matching_receipts_and_zero_debt_generate_proof(self):
        proof = self.run_proof()
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["originToCallbackBlockSeconds"], 2)
        self.assertTrue(proof["callback"].endswith(self.callback))

    def test_debt_or_sweep_failure_prevents_verified_status(self):
        self.assertFalse(self.run_proof(debt=1)["verified"])
        self.assertFalse(self.run_proof(failed=True)["verified"])

    def test_missing_callback_does_not_generate_a_proof(self):
        with self.assertRaisesRegex(RuntimeError, "Base TierChanged"):
            self.run_proof(match=False)

