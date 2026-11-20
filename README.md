# AEGIS

AEGIS is a testnet DeFi risk shield for the Reactive Omni Buildathon. It watches an ERC-20 vault on Base Sepolia and AMM reserve events on Base Sepolia or Unichain Sepolia. A Lasna contract combines outflow, price, and approval signals. A local neural model handles ambiguous cases and the Lasna contract can request an emergency freeze directly.

## Defense tiers

| Tier | Trigger | Base Sepolia action |
| --- | --- | --- |
| 0 Normal | Initial or cold-owner rearm | Operator can withdraw up to the normal cap and set a bounded allowance for one watched spender. |
| 1 Throttled | Model recommends Tier 1 for a score from 40 to 79 | Reduce the cap to 25%, enforce a 30-second cooldown, and revoke the watched allowance. |
| 2 Reserve rebalance | Model recommends Tier 2 for a score from 60 to 79 | Lock operator actions, revoke the watched allowance, and move 25% of the vault's test asset to its cold reserve. |
| 3 Frozen | Deterministic score of at least 80 | Block withdrawals, revoke the watched allowance, and sweep the remaining test asset to the cold reserve. |

Lasna scores outflows over six 10-second buckets using Lasna processing time. A single outflow of 100 demo tokens reaches 80 points. A 5%, 10%, or 20% decline between consecutive valid pool samples contributes 20, 40, or 80 points for 60 seconds. A nonzero watched approval contributes 20 points until a zero approval is observed. Internal transfers to the reserve are excluded. The vault caps withdrawals at 100 tokens in the initial tier and uses the configured ERC-20's smallest units.

For scores of 40 to 79, Lasna emits an anomaly ID, epoch, score breakdown, and 120-second expiry. The Python worker runs a small feed-forward classifier trained on the synthetic scenarios in [risk_model.py](tools/risk_model.py). The model can defer, recommend throttling, or recommend a reserve transfer. The worker posts an EIP-712 signature to the Base registry. Lasna validates the verdict against the original anomaly and tier ceiling. The vault authenticates both the callback proxy and the Reactive contract and independently verifies the signature, expiry, epoch, and tier. A cold-owner rearm advances the epoch, invalidating delayed callbacks from earlier epochs.

## Run locally

Use Python 3.10+ and [Foundry](https://getfoundry.sh/). Fetch the pinned dependency if it is missing.

```powershell
git clone https://github.com/Reactive-Network/reactive-lib-omni.git lib/reactive-lib-omni
git -C lib/reactive-lib-omni checkout 3ade0dcf1d27e67e5783ef807e73a1b9e0c0cce8
forge test -vv
python -m unittest discover -s tools -v
python tools/verdict_worker.py --demo
```

The Foundry tests pass real local ERC-20, pool, and registry logs through a mock Omni system and mock callback proxy. The Python tests exercise live model inference and the worker's signing and relay logic with mocked RPC and Cast calls.

## Deploy to testnet

Import the deployer, hot operator, cold owner, and AI signer test accounts into Foundry using `cast wallet import <account-name> --interactive`. Supply their addresses in the environment. Use a funded Base Sepolia and Lasna deployer, a Base Sepolia hot operator, and a Base Sepolia AI relayer. The AI signer can double as the relayer for the demo. [Get Lasna testnet REACT](https://dev.reactive.network/reactive-mainnet#get-testnet-react). Keep wallet credentials in local keystores. For unattended tools, Foundry can read the keystore password from the path set in `ETH_PASSWORD_FILE`.

```powershell
$env:BASE_RPC_URL = "https://sepolia.base.org"
$env:LASNA_RPC_URL = "https://lasna-rpc.rnk.dev"
$env:AEGIS_DEPLOYER_ACCOUNT = "aegis-deployer"
$env:AEGIS_SIGNER_ACCOUNT = "aegis-ai"
$env:AEGIS_HOT = "<hot operator address>"
$env:AEGIS_COLD = "<cold owner address>"
$env:AEGIS_RESERVE = "<cold reserve address>"
$env:AEGIS_WATCHED = "<watched spender address>"
$env:AEGIS_VERDICT_SIGNER = "<AI signer address>"
python tools/testnet.py preflight
python tools/testnet.py deploy
python tools/testnet.py status
```

Set `UNICHAIN_RPC_URL` before deployment to deploy the pool signal source on Unichain Sepolia. That route also requires Unichain Sepolia ETH for the deployer. The defaults send 1 lREACT into the Lasna contract and 0.005 Base Sepolia ETH into the vault. Override them with `AEGIS_LASNA_RESERVE` and `AEGIS_BASE_RESERVE` if needed. Check reserves and debt before recording. The tool writes deployment addresses and hashes to `deployment.json` and reuses previously recorded deployments when resumed. Wait for the Omni subscriptions to become active before generating demo signals. [Reactive network and proxy addresses](https://dev.reactive.network/origins-and-destinations) ? [Funding and debt](https://dev.reactive.network/economy)

Start the worker in a separate terminal with the same RPC and signer environment.

```powershell
$d = Get-Content deployment.json -Raw | ConvertFrom-Json
$env:AEGIS_REACTIVE = $d.contracts.reactive
$env:AEGIS_REGISTRY = $d.contracts.registry
python tools/verdict_worker.py
```

## Reproduce the demo

In the terminal used for deployment, load the manifest and select the pool RPC.

```powershell
$d = Get-Content deployment.json -Raw | ConvertFrom-Json
$vault = $d.contracts.vault
$pool = $d.contracts.pool
$marketRpc = if ($d.settings.marketChain -eq 1301) { $env:UNICHAIN_RPC_URL } else { $env:BASE_RPC_URL }
```

1. **Benign activity.** The hot operator runs `cast send $vault "withdraw(uint256)" 30ether --rpc-url $env:BASE_RPC_URL --account aegis-hot`. Verify that the vault stays in Tier 0. Wait 60 seconds before the next scenario so the outflow ages out.
2. **AI throttling.** The hot operator sends `grantAllowance(uint256)` with `30ether`, then `withdraw(uint256)` with `25ether`. The two signals should produce a score of 40. Capture the Lasna anomaly, the worker's Base verdict transaction, the second Lasna reaction, and the Tier 1 callback. Attempt a withdrawal before the cooldown ends and verify it reverts.
3. **AI reserve transfer.** Call `rearm()` using the cold-owner account. Wait for Lasna to observe the new epoch before generating signals. The deployer sends `setReserves(uint112,uint112)` with `100 100` to `$pool` using `$marketRpc`. The hot operator grants a 30-token allowance. The deployer sends reserves `100 90`. The pool drop and allowance should produce a score of 60 and a Tier 2 callback. Verify that 25% of the remaining test asset reaches the cold reserve and the operator is locked.
4. **Emergency without AI.** Stop the worker and send pool reserves `100 70`. The deterministic price shock should trigger Tier 3, sweep the remainder, and block all operator withdrawals. Capture the origin event, Lasna reaction, and Base callback.

Use `python tools/testnet.py status` after each step. For a submission, record transaction links and measured elapsed time for each complete loop. [Base Sepolia explorer](https://sepolia.basescan.org/) ? [Lasna explorer](https://lasna.reactscan.net/) ? [Unichain Sepolia explorer](https://sepolia.uniscan.xyz/)

## Verify the emergency loop

On a fresh Tier 0 vault with at least 100 test tokens, record the Lasna head and submit a single threshold withdrawal. Run this as a separate emergency proof or after a cold-owner rearm has reached Lasna. The AI worker can be stopped.

```powershell
$d = Get-Content deployment.json -Raw | ConvertFrom-Json
$before = cast block-number --rpc-url $env:LASNA_RPC_URL
$receipt = cast send $d.contracts.vault "withdraw(uint256)" "100000000000000000000" --rpc-url $env:BASE_RPC_URL --account aegis-hot --json | ConvertFrom-Json
python tools/verify_emergency.py --origin $receipt.transactionHash --from-lasna-block $before --output emergency-proof.json
```

The verifier matches the origin receipt to the Lasna emergency request and the Base Tier 3 event for the same transaction hash and epoch. It checks the current tier, sweep result, and debt on both chains, and emits explorer links and a latency estimate from whole-second block timestamps. Run it before rearming the vault. Use the official [Reactive debugging guide](https://dev.reactive.network/debugging) if the callback times out.

## Demo scope

The test token and controlled pool publisher provide reproducible market and wallet signals. Tier 2 transfers a portion of the test asset into a cold reserve. Its scope is limited to reserve allocation. The model uses synthetic scenario labels. The callback is asynchronous and any initiating withdrawal has already executed when the freeze lands. An asset-side revocation or transfer failure preserves the protective tier and emits a failure event. The cold owner can retry the failed action.

The public testnet loops, balances, callback debt, and timing still require verification with funded accounts. The repository ships a command-line telemetry view.


