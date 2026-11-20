// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {AbstractReactive} from "@reactive/src/base/AbstractReactive.sol";
import {ISystemContract} from "@reactive/src/interfaces/ISystemContract.sol";
import {AegisVault} from "./AegisVault.sol";
import {AegisVerdict} from "./AegisVerdict.sol";

contract AegisReactive is AbstractReactive {
    error InvalidConfiguration();
    error NotConfigurator();

    event AnomalyDetected(
        bytes32 indexed anomalyId,
        uint64 indexed epoch,
        uint8 score,
        uint8 outflowPoints,
        uint8 marketPoints,
        uint8 allowancePoints,
        uint64 expiresAt
    );
    event EmergencyRequested(uint64 indexed epoch, bytes32 indexed originTxHash, uint8 score);

    uint256 public constant BASE_SEPOLIA = 84532;
    uint256 public constant UNICHAIN_SEPOLIA = 1301;
    uint256 public constant TRANSFER_TOPIC = uint256(keccak256("Transfer(address,address,uint256)"));
    uint256 public constant APPROVAL_TOPIC = uint256(keccak256("Approval(address,address,uint256)"));
    uint256 public constant SYNC_TOPIC = uint256(keccak256("Sync(uint112,uint112)"));
    uint256 public constant REARM_TOPIC = uint256(keccak256("Rearmed(uint64)"));
    uint256 public constant VERDICT_TOPIC =
        uint256(keccak256("AIRiskVerdictPosted(bytes32,uint64,uint8,uint64,bytes32,bytes32,uint8)"));

    uint256 private constant BUCKET_SECONDS = 10;
    uint256 private constant BUCKET_COUNT = 6;
    uint256 private constant VERDICT_TTL = 120;

    struct Configuration {
        address asset;
        address registry;
        address reserve;
        address spender;
        address pool;
        uint256 poolChain;
        uint128 criticalAmount;
    }

    struct Bucket {
        uint128 number;
        uint128 amount;
    }

    struct Pending {
        uint64 epoch;
        uint64 expiresAt;
        uint8 maxTier;
    }

    address public immutable configurator;
    address public immutable asset;
    address public immutable registry;
    address public immutable reserve;
    address public immutable spender;
    address public immutable pool;
    uint256 public immutable poolChain;
    uint128 public immutable criticalAmount;

    address public vault;
    uint64 public epoch = 1;
    uint256 private rearmBlock;
    uint256 private rearmLogIndex;
    uint256 private previousPrice;
    uint256 private marketExpiresAt;
    uint8 private marketPoints;
    bool private riskyAllowance;
    Bucket[BUCKET_COUNT] private outflows;

    mapping(bytes32 => bool) private processed;
    mapping(bytes32 => Pending) public pending;

    constructor(Configuration memory config) payable {
        if (
            config.asset == address(0) || config.registry == address(0) || config.reserve == address(0)
                || config.spender == address(0) || config.pool == address(0) || config.criticalAmount == 0
                || (config.poolChain != BASE_SEPOLIA && config.poolChain != UNICHAIN_SEPOLIA)
        ) {
            revert InvalidConfiguration();
        }
        configurator = msg.sender;
        asset = config.asset;
        registry = config.registry;
        reserve = config.reserve;
        spender = config.spender;
        pool = config.pool;
        poolChain = config.poolChain;
        criticalAmount = config.criticalAmount;
    }

    function configureVault(address vault_) external {
        if (msg.sender != configurator) revert NotConfigurator();
        if (vault != address(0) || vault_ == address(0)) revert InvalidConfiguration();

        vault = vault_;
        uint256 vaultTopic = uint256(uint160(vault_));
        SYSTEM.subscribe(BASE_SEPOLIA, asset, TRANSFER_TOPIC, vaultTopic, REACTIVE_IGNORE, REACTIVE_IGNORE);
        SYSTEM.subscribe(BASE_SEPOLIA, asset, APPROVAL_TOPIC, vaultTopic, uint256(uint160(spender)), REACTIVE_IGNORE);
        SYSTEM.subscribe(BASE_SEPOLIA, vault_, REARM_TOPIC, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE);
        SYSTEM.subscribe(BASE_SEPOLIA, registry, VERDICT_TOPIC, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE);
        SYSTEM.subscribe(poolChain, pool, SYNC_TOPIC, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE);
    }

    function react(LogRecord memory log) external override onlySystem {
        if (vault == address(0)) return;
        if (log.chainId == BASE_SEPOLIA && log.contractAddress == registry && log.topic0 == VERDICT_TOPIC) {
            _handleVerdict(log);
            return;
        }
        if (log.chainId == BASE_SEPOLIA && log.contractAddress == vault && log.topic0 == REARM_TOPIC) {
            _observeRearm(log);
            return;
        }
        if (
            log.chainId == BASE_SEPOLIA && epoch > 1
                && (log.blockNumber < rearmBlock || (log.blockNumber == rearmBlock && log.logIndex <= rearmLogIndex))
        ) return;

        bytes32 evidence = keccak256(abi.encode(log.chainId, log.txHash, log.logIndex));
        if (processed[evidence]) return;
        if (
            log.chainId == BASE_SEPOLIA && log.contractAddress == asset && log.topic1 == uint256(uint160(vault))
                && log.data.length == 32
        ) {
            uint256 amount = abi.decode(log.data, (uint256));
            if (
                log.topic0 == TRANSFER_TOPIC && log.topic2 != uint256(uint160(reserve))
                    && log.topic2 != uint256(uint160(vault)) && amount != 0
            ) {
                _recordOutflow(amount);
            } else if (log.topic0 == APPROVAL_TOPIC && log.topic2 == uint256(uint160(spender))) {
                riskyAllowance = amount != 0;
                if (!riskyAllowance) {
                    processed[evidence] = true;
                    return;
                }
            } else {
                return;
            }
        } else if (
            log.chainId == poolChain && log.contractAddress == pool && log.topic0 == SYNC_TOPIC && log.data.length == 64
        ) {
            (uint256 reserve0, uint256 reserve1) = abi.decode(log.data, (uint256, uint256));
            if (reserve0 == 0 || reserve1 == 0 || reserve0 > type(uint112).max || reserve1 > type(uint112).max) return;
            processed[evidence] = true;
            if (!_recordPrice(reserve0, reserve1)) return;
        } else {
            return;
        }
        processed[evidence] = true;
        _assess(evidence, bytes32(log.txHash));
    }

    function risk() public view returns (uint8 score, uint8 outflow, uint8 market, uint8 allowance) {
        uint256 current = block.timestamp / BUCKET_SECONDS;
        uint256 total;
        for (uint256 index; index < BUCKET_COUNT; index++) {
            Bucket memory bucket = outflows[index];
            if (current - bucket.number < BUCKET_COUNT) total += bucket.amount;
        }
        outflow = total >= criticalAmount ? 80 : uint8(total * 80 / criticalAmount);
        market = block.timestamp < marketExpiresAt ? marketPoints : 0;
        allowance = riskyAllowance ? 20 : 0;
        uint256 sum = uint256(outflow) + market + allowance;
        score = sum > 100 ? 100 : uint8(sum);
    }

    function coverDebt() external {
        _coverDebt();
    }

    function _observeRearm(LogRecord memory log) private {
        if (log.topic1 <= epoch || log.topic1 > type(uint64).max || log.data.length != 0) return;
        epoch = uint64(log.topic1);
        rearmBlock = log.blockNumber;
        rearmLogIndex = log.logIndex;
        delete outflows;
        marketExpiresAt = 0;
        riskyAllowance = false;
    }

    function _recordOutflow(uint256 amount) private {
        uint128 current = uint128(block.timestamp / BUCKET_SECONDS);
        Bucket storage bucket = outflows[current % BUCKET_COUNT];
        uint256 previous = bucket.number == current ? bucket.amount : 0;
        uint256 available = criticalAmount - previous;
        bucket.number = current;
        bucket.amount = uint128(previous + (amount < available ? amount : available));
    }

    function _recordPrice(uint256 reserve0, uint256 reserve1) private returns (bool) {
        uint256 last = previousPrice;
        uint256 price = (reserve1 << 112) / reserve0;
        previousPrice = price;
        if (price >= last) return false;
        uint256 drop = (last - price) * 10_000 / last;
        uint8 points = drop >= 2_000 ? 80 : drop >= 1_000 ? 40 : drop >= 500 ? 20 : 0;
        if (points == 0) return false;
        if (block.timestamp >= marketExpiresAt || points >= marketPoints) {
            marketPoints = points;
            marketExpiresAt = block.timestamp + BUCKET_SECONDS * BUCKET_COUNT;
        }
        return true;
    }

    function _assess(bytes32 anomalyId, bytes32 originTxHash) private {
        (uint8 score, uint8 outflow, uint8 market, uint8 allowance) = risk();
        if (score >= 80) {
            _request(abi.encodeCall(AegisVault.freeze, (address(0), epoch, originTxHash)));
            emit EmergencyRequested(epoch, originTxHash, score);
        } else if (score >= 40) {
            uint64 expiresAt = uint64(block.timestamp + VERDICT_TTL);
            pending[anomalyId] = Pending(epoch, expiresAt, score >= 60 ? 2 : 1);
            emit AnomalyDetected(anomalyId, epoch, score, outflow, market, allowance, expiresAt);
        }
    }

    function _handleVerdict(LogRecord memory log) private {
        bytes32 anomalyId = bytes32(log.topic1);
        Pending memory request = pending[anomalyId];
        if (request.epoch != epoch || block.timestamp >= request.expiresAt || log.data.length != 192) return;

        (uint256 sourceEpoch, uint256 tier, uint256 deadline, bytes32 r, bytes32 s, uint256 v) =
            abi.decode(log.data, (uint256, uint256, uint256, bytes32, bytes32, uint256));
        if (
            sourceEpoch != epoch || tier == 0 || tier > request.maxTier || deadline > request.expiresAt
                || block.timestamp >= deadline || (v != 27 && v != 28)
        ) return;

        delete pending[anomalyId];
        AegisVerdict.Verdict memory verdict = AegisVerdict.Verdict(anomalyId, epoch, uint8(tier), uint64(deadline));
        AegisVerdict.Signature memory signature = AegisVerdict.Signature(r, s, uint8(v));
        _request(abi.encodeCall(AegisVault.applyVerdict, (address(0), verdict, signature)));
    }

    function _request(bytes memory payload) private {
        SYSTEM.requestCallbackV_1_0(
            ISystemContract.CallbackConfiguration_V_1_0({
                chainId: BASE_SEPOLIA, recipient: vault, gasLimit: 1_000_000, payload: payload
            })
        );
    }
}
