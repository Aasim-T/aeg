"""Deploy the AEGIS testnet demo and inspect its live configuration."""

import argparse
import json
import os
from pathlib import Path
import subprocess

from verdict_worker import address, cast, rpc


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deployment.json"
BASE_PROXY = "0x476F44b11F5C942c58f4c8E3d0300E5710743585"
LASNA_SYSTEM = "0x8888888888888888888888888888888888888888"
TOKEN = 10**18
CRITICAL = 100 * TOKEN
DEMO_BALANCE = 1_000 * TOKEN


def write_manifest(manifest):
    temporary = MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(MANIFEST)


def checked_rpc(variable, chain):
    url = os.environ[variable]
    if int(rpc(url, "eth_chainId"), 16) != chain:
        raise ValueError(f"{variable} must target chain {chain}")
    return url


def call(url, target, signature, *args):
    return cast("call", target, signature, *args, "--rpc-url", url)


def integer(result):
    # Newer Cast versions append a human-readable scientific notation.
    return int(result.split()[0], 0)


def testnet_balance(url, account):
    return int(rpc(url, "eth_getBalance", [account, "latest"]), 16)


def preflight(base, lasna, market, market_chain):
    deployer = address(cast("wallet", "address", "--account", os.environ["AEGIS_DEPLOYER_ACCOUNT"]))
    hot = address(os.environ["AEGIS_HOT"])
    report = {
        "deployer": deployer,
        "baseDeployerWei": testnet_balance(base, deployer),
        "lasnaDeployerWei": testnet_balance(lasna, deployer),
        "baseHotOperatorWei": testnet_balance(base, hot),
        "baseProxyAvailable": rpc(base, "eth_getCode", [BASE_PROXY, "latest"]) != "0x",
        "lasnaSystemAvailable": rpc(lasna, "eth_getCode", [LASNA_SYSTEM, "latest"]) != "0x",
    }
    if market_chain != 84532:
        report["unichainDeployerWei"] = testnet_balance(market, deployer)
    print(json.dumps(report, indent=2))
    # Nonzero balances still need to cover transaction fees and configured deposits.
    return all(report.values())


def deploy(base, lasna, market, market_chain):
    account = os.environ["AEGIS_DEPLOYER_ACCOUNT"]
    roles = {
        key: address(os.environ[variable]).lower()
        for key, variable in (
            ("hot", "AEGIS_HOT"), ("cold", "AEGIS_COLD"), ("reserve", "AEGIS_RESERVE"),
            ("spender", "AEGIS_WATCHED"), ("signer", "AEGIS_VERDICT_SIGNER"),
        )
    }
    if roles["hot"] in (roles["cold"], roles["reserve"]) or roles["spender"] == roles["reserve"]:
        raise ValueError("Choose separate hot and cold roles and a spender other than the reserve")

    sender = address(cast("wallet", "address", "--account", account)).lower()
    settings = {"roles": roles, "deployer": sender, "marketChain": market_chain}
    data = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {
        "settings": settings, "contracts": {}, "transactions": {},
    }
    if data["settings"] != settings:
        raise ValueError("Existing deployment.json has different settings")
    contracts, transactions = data["contracts"], data["transactions"]

    if (rpc(base, "eth_getCode", [BASE_PROXY, "latest"]) == "0x"
            or rpc(lasna, "eth_getCode", [LASNA_SYSTEM, "latest"]) == "0x"):
        raise ValueError("Omni system or callback proxy code is unavailable")

    def create(key, url, contract, args=(), value=None):
        if key in contracts:
            if rpc(url, "eth_getCode", [address(contracts[key]), "latest"]) == "0x":
                raise ValueError(f"Manifest contract {key} is missing on its configured chain")
            return contracts[key]
        command = ["forge", "create", contract, "--rpc-url", url, "--account", account, "--broadcast", "--json"]
        if value is not None:
            command.extend(("--value", value))
        if args:
            command.extend(("--constructor-args", *args))
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Could not deploy {key} (Forge exit {result.returncode}). Check the deployer balance and RPC.")
        try:
            # Forge can prepend build output to the JSON deployment result.
            record = json.loads(result.stdout[result.stdout.index("{"):])
            deployed = address(record["deployedTo"])
            tx = record["transactionHash"]
        except (ValueError, KeyError) as error:
            raise RuntimeError(f"{key} may have deployed. Inspect the deployer history before retrying.") from error
        contracts[key], transactions[key] = deployed, tx
        write_manifest(data)
        print(f"{key} {deployed} {tx}", flush=True)
        return deployed

    asset = create("asset", base, "src/AegisDemoSignals.sol:DemoAsset")
    pool = create("pool", market, "src/AegisDemoSignals.sol:DemoPool")
    registry = create("registry", base, "src/AegisVerdictRegistry.sol:AegisVerdictRegistry", (roles["signer"],))
    reactive_config = f"({asset},{registry},{roles['reserve']},{roles['spender']},{pool},{market_chain},{CRITICAL})"
    reactive = create("reactive", lasna, "src/AegisReactive.sol:AegisReactive", (reactive_config,),
                      os.environ.get("AEGIS_LASNA_RESERVE", "1ether"))
    vault_config = f"({asset},{roles['hot']},{roles['cold']},{roles['reserve']},{roles['spender']},{CRITICAL})"
    vault = create("vault", base, "src/AegisVault.sol:AegisVault", (BASE_PROXY, reactive, registry, vault_config),
                   os.environ.get("AEGIS_BASE_RESERVE", "0.005ether"))

    def send(key, url, target, signature, *args):
        result = cast("send", target, signature, *args, "--rpc-url", url, "--account", account, "--json")
        receipt = json.loads(result)
        if integer(receipt["status"]) != 1:
            raise RuntimeError(f"{key} transaction reverted")
        transactions[key] = receipt["transactionHash"]
        write_manifest(data)
        print(f"{key} {transactions[key]}", flush=True)

    bound_vault = call(lasna, reactive, "vault()(address)")
    if int(bound_vault, 16) == 0:
        send("configureVault", lasna, reactive, "configureVault(address)", vault)
    elif bound_vault.lower() != vault.lower():
        raise RuntimeError("Reactive contract is already bound to a different vault")

    if "fundVault" not in transactions:
        balance = integer(call(base, asset, "balanceOf(address)(uint256)", vault))
        if balance < DEMO_BALANCE:
            send("fundVault", base, asset, "transfer(address,uint256)", vault, DEMO_BALANCE - balance)
    print(f"Saved deployment to {MANIFEST}")


def status(base, lasna):
    if not MANIFEST.exists():
        raise ValueError("Deploy the demo to create deployment.json first")
    targets = json.loads(MANIFEST.read_text(encoding="utf-8"))["contracts"]
    vault, reactive, asset = (address(targets[key]) for key in ("vault", "reactive", "asset"))
    score = call(lasna, reactive, "risk()(uint8,uint8,uint8,uint8)").splitlines()
    summary = {
        "vault": vault,
        "vaultEpoch": integer(call(base, vault, "epoch()(uint64)")),
        "reactiveEpoch": integer(call(lasna, reactive, "epoch()(uint64)")),
        "tier": integer(call(base, vault, "tier()(uint8)")),
        "threatScore": integer(score[0]),
        "outflowPoints": integer(score[1]),
        "marketPoints": integer(score[2]),
        "approvalPoints": integer(score[3]),
        "testAssetBalance": integer(call(base, asset, "balanceOf(address)(uint256)", vault)),
        "vaultNativeBalanceWei": int(rpc(base, "eth_getBalance", [vault, "latest"]), 16),
        "reactiveNativeBalanceWei": int(rpc(lasna, "eth_getBalance", [reactive, "latest"]), 16),
        "baseCallbackReservesWei": integer(call(base, BASE_PROXY, "reserves(address)(uint256)", vault)),
        "lasnaReservesWei": integer(call(lasna, LASNA_SYSTEM, "reserves(address)(uint256)", reactive)),
        "baseCallbackDebtWei": integer(call(base, BASE_PROXY, "debt(address)(uint256)", vault)),
        "lasnaDebtWei": integer(call(lasna, LASNA_SYSTEM, "debt(address)(uint256)", reactive)),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "deploy", "status"))
    options = parser.parse_args()
    try:
        base = checked_rpc("BASE_RPC_URL", 84532)
        lasna = checked_rpc("LASNA_RPC_URL", 5318007)
        if options.action == "status":
            status(base, lasna)
        else:
            market_chain = 1301 if "UNICHAIN_RPC_URL" in os.environ else 84532
            market = checked_rpc("UNICHAIN_RPC_URL", 1301) if market_chain == 1301 else base
            if options.action == "preflight":
                if not preflight(base, lasna, market, market_chain):
                    parser.exit(1, "Preflight found an unfunded account or missing system contract.\n")
            else:
                deploy(base, lasna, market, market_chain)
    except KeyError as error:
        parser.exit(2, f"Missing configuration or deployment entry {error.args[0]}\n")
    except (ValueError, RuntimeError) as error:
        parser.exit(2, f"{error}\n")
    except (OSError, subprocess.TimeoutExpired) as error:
        parser.exit(2, f"Network or process failure ({type(error).__name__}). Check Foundry and your RPCs.\n")


