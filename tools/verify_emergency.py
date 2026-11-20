"""Verify an AEGIS Tier 3 testnet loop from its Base Sepolia origin transaction."""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from testnet import BASE_PROXY, LASNA_SYSTEM, MANIFEST, call, checked_rpc, integer
from verdict_worker import address, cast, rpc


EMERGENCY = "EmergencyRequested(uint64,bytes32,uint8)"
TIER_CHANGED = "TierChanged(uint64,uint8,bytes32)"
PROTECTION_FAILED = "ProtectionActionFailed(uint64,uint8,bytes4)"
TRANSFER = "Transfer(address,address,uint256)"
BASE_EXPLORER = "https://sepolia.basescan.org/tx/"
LASNA_EXPLORER = "https://lasna.reactscan.net/tx/"


def tx_hash(value):
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("Expected a 32-byte transaction hash")
    return value.lower()


def abi_word(value):
    return f"0x{int(value, 16):064x}"


def block_time(url, block_number):
    return int(rpc(url, "eth_getBlockByNumber", [block_number, False])["timestamp"], 16)


def observed_logs(url, contract, topics, start, end):
    for first in range(start, end + 1, 100):
        for log in rpc(url, "eth_getLogs", [{
            "address": contract,
            "topics": topics,
            "fromBlock": hex(first),
            "toBlock": hex(min(first + 99, end)),
        }]):
            if not log.get("removed"):
                yield log


def successful_receipt(url, tx):
    receipt = rpc(url, "eth_getTransactionReceipt", [tx])
    if not receipt or int(receipt["status"], 16) != 1:
        raise ValueError(f"Transaction has no successful receipt: {tx}")
    return receipt


def verify_origin(receipt, asset, vault, reserve, transfer_topic):
    # This verifier uses a Base vault outflow as the trigger for its emergency proof.
    for log in receipt["logs"]:
        topics = [topic.lower() for topic in log["topics"]]
        if (log["address"].lower() == asset.lower() and len(topics) == 3
                and topics[:2] == [transfer_topic, abi_word(vault)]
                and topics[2] not in (abi_word(vault), abi_word(reserve))
                and int(log["data"], 16) > 0):
            return
    raise ValueError("Origin receipt does not contain an outbound AEGIS vault token transfer")


def verify(base, lasna, manifest, origin, start, timeout):
    contracts = manifest["contracts"]
    vault, asset, reactive = (address(contracts[key]) for key in ("vault", "asset", "reactive"))
    reserve = address(manifest["settings"]["roles"]["reserve"])

    origin = tx_hash(origin)
    receipt = successful_receipt(base, origin)
    verify_origin(receipt, asset, vault, reserve, cast("keccak", TRANSFER).lower())
    base_start = int(receipt["blockNumber"], 16)
    base_cursor = base_start
    lasna_head = int(rpc(lasna, "eth_blockNumber"), 16)
    lasna_cursor = max(0, lasna_head - 300) if start is None else start
    lasna_topics = [cast("keccak", EMERGENCY), None, origin]
    callback_topics = [cast("keccak", TIER_CHANGED), None, origin]
    failure_topic = cast("keccak", PROTECTION_FAILED).lower()
    reaction = callback = None
    end = time.monotonic() + timeout
    while True:
        if reaction is None:
            lasna_end = int(rpc(lasna, "eth_blockNumber"), 16)
            reaction = next(observed_logs(lasna, reactive, lasna_topics, lasna_cursor, lasna_end), None)
            lasna_cursor = max(lasna_cursor, lasna_end + 1)
            if reaction is not None:
                score = int(reaction["data"], 16)
                if not 80 <= score <= 100:
                    raise ValueError("Unexpected emergency score in Lasna event")

        if reaction is not None:
            callback_topics[1] = reaction["topics"][1]
            base_end = int(rpc(base, "eth_blockNumber"), 16)
            callback = next((
                log for log in observed_logs(base, vault, callback_topics, base_cursor, base_end)
                if int(log["data"], 16) == 3
            ), None)
            if callback is not None:
                break
            base_cursor = max(base_cursor, base_end + 1)

        if time.monotonic() >= end:
            missing = "Lasna EmergencyRequested" if reaction is None else "Base TierChanged callback"
            raise RuntimeError(f"Timed out waiting for {missing}. Run testnet.py status to inspect debt and reserves.")
        time.sleep(2)

    reaction_tx = tx_hash(reaction["transactionHash"])
    callback_tx = tx_hash(callback["transactionHash"])
    successful_receipt(lasna, reaction_tx)
    callback_receipt = successful_receipt(base, callback_tx)
    failures = [
        log for log in callback_receipt["logs"]
        if log["address"].lower() == vault.lower() and log["topics"] and log["topics"][0].lower() == failure_topic
    ]

    origin_time = block_time(base, receipt["blockNumber"])
    reaction_time = block_time(lasna, reaction["blockNumber"])
    callback_time = block_time(base, callback["blockNumber"])
    base_debt = integer(call(base, BASE_PROXY, "debt(address)(uint256)", vault))
    lasna_debt = integer(call(lasna, LASNA_SYSTEM, "debt(address)(uint256)", reactive))
    remaining = integer(call(base, asset, "balanceOf(address)(uint256)", vault))
    current_tier = integer(call(base, vault, "tier()(uint8)"))
    current_epoch = integer(call(base, vault, "epoch()(uint64)"))
    proof_epoch = int(reaction["topics"][1], 16)

    return {
        "verified": not failures and base_debt == lasna_debt == remaining == 0
        and current_tier == 3 and current_epoch == proof_epoch,
        "epoch": proof_epoch,
        "currentEpoch": current_epoch,
        "currentTier": current_tier,
        "threatScore": int(reaction["data"], 16),
        "origin": BASE_EXPLORER + origin,
        "reaction": LASNA_EXPLORER + reaction_tx,
        "callback": BASE_EXPLORER + callback_tx,
        "blockTimestampsUnix": {"origin": origin_time, "reaction": reaction_time, "callback": callback_time},
        "originToCallbackBlockSeconds": callback_time - origin_time,
        "baseCallbackDebtWei": base_debt,
        "lasnaDebtWei": lasna_debt,
        "vaultTestAssetRemaining": remaining,
        "protectionActionFailures": len(failures),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, type=tx_hash)
    parser.add_argument("--from-lasna-block", type=int, help="Lasna block recorded before the origin transaction")
    parser.add_argument("--timeout", type=int, default=90, help="Seconds to wait for the reaction and callback")
    parser.add_argument("--output", type=Path, help="Optional path for the JSON proof")
    args = parser.parse_args()
    if args.timeout < 0 or (args.from_lasna_block is not None and args.from_lasna_block < 0):
        parser.error("Timeout and starting block must be nonnegative")

    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        proof = verify(
            checked_rpc("BASE_RPC_URL", 84532), checked_rpc("LASNA_RPC_URL", 5318007),
            manifest, args.origin, args.from_lasna_block, args.timeout,
        )
        report = json.dumps(proof, indent=2) + "\n"
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        print(report, end="")
        return 0 if proof["verified"] else 1
    except KeyError as error:
        parser.exit(2, f"Missing configuration or deployment entry {error.args[0]}\n")
    except (RuntimeError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    except (OSError, subprocess.TimeoutExpired) as error:
        parser.exit(2, f"Could not read deployment, reach testnet, or run Cast ({type(error).__name__}).\n")


if __name__ == "__main__":
    raise SystemExit(main())
