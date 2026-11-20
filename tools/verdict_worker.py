"""Watch Lasna anomalies and relay bounded, signed verdicts to Base Sepolia."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass

from risk_model import RiskModel


ANOMALY_EVENT = "AnomalyDetected(bytes32,uint64,uint8,uint8,uint8,uint8,uint64)"
POST_VERDICT = "post(bytes32,uint64,uint8,uint64,bytes32,bytes32,uint8)"


def rpc(url, method, params=()):
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if "error" in payload:
        raise RuntimeError(f"{method} failed with RPC code {payload['error'].get('code')}")
    return payload["result"]


def cast(*args):
    result = subprocess.run(
        ["cast", *map(str, args)], capture_output=True, text=True, timeout=45, check=False
    )
    if result.returncode != 0:
        # Cast may echo credential-bearing URLs in its error output.
        raise RuntimeError(f"cast {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def address(value):
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value) or int(value, 16) == 0:
        raise ValueError("Expected a nonzero Ethereum address")
    return value


@dataclass(frozen=True)
class Anomaly:
    id: str
    epoch: int
    outflow: int
    market: int
    allowance: int
    expiry: int

    @classmethod
    def from_log(cls, log):
        _, anomaly_id, epoch = log["topics"]
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", anomaly_id):
            raise ValueError("Invalid anomaly ID")
        raw = log["data"].removeprefix("0x")
        if len(raw) != 5 * 64:
            raise ValueError("Invalid anomaly event data")
        score, outflow, market, allowance, expiry = (
            int(raw[offset:offset + 64], 16) for offset in range(0, len(raw), 64)
        )
        if not (score == outflow + market + allowance and 40 <= score < 80
                and 0 <= outflow <= 80 and market in (0, 20, 40) and allowance in (0, 20)):
            raise ValueError("Invalid anomaly risk breakdown")
        return cls(anomaly_id, int(epoch, 16), outflow, market, allowance, expiry)


def unpack_signature(result):
    if not re.fullmatch(r"0x[0-9a-fA-F]{130}", result):
        raise ValueError("Cast returned an invalid signature")
    recovery_id = int(result[-2:], 16)
    if recovery_id not in (0, 1, 27, 28):
        raise ValueError("Invalid signature recovery ID")
    if recovery_id < 2:
        recovery_id += 27
    return result[:66], "0x" + result[66:130], recovery_id


def handle(anomaly, model, base, registry, signer, relayer):
    tier = model.recommend(anomaly.outflow, anomaly.market, anomaly.allowance)
    print(f"{anomaly.id} epoch={anomaly.epoch} model recommendation={tier}", flush=True)
    if tier == 0:
        return

    base_now = int(rpc(base, "eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
    if anomaly.expiry - base_now <= 10:
        return
    if cast("call", registry, "posted(bytes32)(bool)", anomaly.id, "--rpc-url", base) == "true":
        return

    values = (anomaly.id, anomaly.epoch, tier, anomaly.expiry)
    digest = cast(
        "call", registry, "digest(bytes32,uint64,uint8,uint64)(bytes32)", *values, "--rpc-url", base
    )
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", digest):
        raise ValueError("Registry returned an invalid digest")
    signature = unpack_signature(cast("wallet", "sign", "--no-hash", "--account", signer, digest))
    tx = cast("send", registry, POST_VERDICT, *values, *signature,
              "--account", relayer, "--rpc-url", base, "--async")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx):
        raise RuntimeError("Cast returned an invalid transaction hash")
    print(f"Base verdict transaction {tx}", flush=True)

    for _ in range(15):
        receipt = rpc(base, "eth_getTransactionReceipt", [tx])
        if receipt is not None:
            if int(receipt["status"], 16) != 1:
                raise RuntimeError("Verdict transaction reverted")
            return
        time.sleep(1)
    raise RuntimeError("Verdict has not confirmed yet")


def run(start_block):
    base, lasna = os.environ["BASE_RPC_URL"], os.environ["LASNA_RPC_URL"]
    reactive = address(os.environ["AEGIS_REACTIVE"])
    registry = address(os.environ["AEGIS_REGISTRY"])
    signer = os.environ["AEGIS_SIGNER_ACCOUNT"]
    relayer = os.environ.get("AEGIS_RELAYER_ACCOUNT", signer)
    if int(rpc(base, "eth_chainId"), 16) != 84532 or int(rpc(lasna, "eth_chainId"), 16) != 5318007:
        raise ValueError("Use a Base Sepolia RPC and a Lasna RPC")
    expected = cast("call", registry, "signer()(address)", "--rpc-url", base)
    actual = cast("wallet", "address", "--account", signer)
    if address(expected).lower() != address(actual).lower():
        raise ValueError("The signer account does not match the registry")
    if address(cast("call", reactive, "registry()(address)", "--rpc-url", lasna)).lower() != registry.lower():
        raise ValueError("The Reactive contract does not recognize this registry")

    model = RiskModel()
    topic = cast("keccak", ANOMALY_EVENT)
    cursor = int(rpc(lasna, "eth_blockNumber"), 16) if start_block is None else start_block
    print(f"Watching Lasna for {reactive} from block {cursor}", flush=True)
    while True:
        try:
            latest = int(rpc(lasna, "eth_blockNumber"), 16)
            if cursor <= latest:
                end = min(latest, cursor + 99)
                logs = rpc(lasna, "eth_getLogs", [{
                    "address": reactive, "topics": [topic], "fromBlock": hex(cursor), "toBlock": hex(end),
                }])
                for log in logs:
                    if not log.get("removed"):
                        handle(Anomaly.from_log(log), model, base, registry, signer, relayer)
                cursor = end + 1
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"Waiting to retry block {cursor} ({type(error).__name__})", file=sys.stderr, flush=True)
            time.sleep(5)
        time.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-block", type=int, help="First Lasna block to scan")
    parser.add_argument("--demo", action="store_true", help="Run the neural model locally without a wallet")
    options = parser.parse_args()
    try:
        if options.demo:
            model = RiskModel()
            for features in ((40, 0, 0), (20, 0, 20), (0, 40, 20), (80, 0, 0)):
                print(f"outflow={features[0]} market={features[1]} approval={features[2]} "
                      f"recommendation={model.recommend(*features)}")
        else:
            if options.from_block is not None and options.from_block < 0:
                parser.error("--from-block must be nonnegative")
            run(options.from_block)
    except KeyboardInterrupt:
        pass
    except KeyError as error:
        parser.exit(2, f"Missing configuration {error.args[0]}\n")
    except FileNotFoundError:
        parser.exit(2, "Install Foundry and put cast on PATH.\n")
    except (RuntimeError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    except (OSError, subprocess.TimeoutExpired) as error:
        parser.exit(2, f"Network or process failure ({type(error).__name__})\n")

