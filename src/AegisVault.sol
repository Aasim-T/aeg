// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {AbstractCallback} from "@reactive/src/base/AbstractCallback.sol";
import {IPayable} from "@reactive/src/interfaces/IPayable.sol";
import {AegisVerdict} from "./AegisVerdict.sol";
import {AegisVerdictRegistry} from "./AegisVerdictRegistry.sol";

interface IAegisAsset {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address recipient, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

contract AegisVault is AbstractCallback {
    error InvalidConfiguration();
    error NotOperator();
    error NotColdOwner();
    error Frozen();
    error OperatorLocked();
    error WithdrawalLimited();
    error AssetActionFailed();
    error InternalActionOnly();

    event TierChanged(uint64 indexed epoch, uint8 tier, bytes32 indexed evidence);
    event Rearmed(uint64 indexed epoch);
    event ProtectionActionFailed(uint64 indexed epoch, uint8 tier, bytes4 action);

    struct Configuration {
        IAegisAsset asset;
        address operator;
        address coldOwner;
        address reserve;
        address watchedSpender;
        uint256 normalCap;
    }

    uint256 public constant THROTTLE_COOLDOWN = 30;

    IAegisAsset public immutable asset;
    address public immutable operator;
    address public immutable coldOwner;
    address public immutable reserve;
    address public immutable watchedSpender;
    uint256 public immutable normalCap;
    address public immutable verdictSigner;
    bytes32 public immutable verdictDomain;

    uint64 public epoch = 1;
    uint8 public tier;
    uint256 public lastWithdrawal;
    bool public rebalanced;

    constructor(IPayable proxy, address reactive, AegisVerdictRegistry registry, Configuration memory config)
        payable
        AbstractCallback(proxy, reactive)
    {
        if (
            address(proxy) == address(0) || reactive == address(0) || address(config.asset).code.length == 0
                || config.operator == address(0) || config.coldOwner == address(0) || config.reserve == address(0)
                || config.operator == config.coldOwner || config.operator == config.reserve
                || config.reserve == address(this) || config.watchedSpender == address(0)
                || config.watchedSpender == config.reserve || config.normalCap < 4
        ) revert InvalidConfiguration();
        asset = config.asset;
        operator = config.operator;
        coldOwner = config.coldOwner;
        reserve = config.reserve;
        watchedSpender = config.watchedSpender;
        normalCap = config.normalCap;
        verdictSigner = registry.signer();
        verdictDomain = registry.domainSeparator();
    }

    function frozen() external view returns (bool) {
        return tier == 3;
    }

    function withdraw(uint256 amount) external {
        if (tier == 3) revert Frozen();
        if (tier == 2) revert OperatorLocked();
        if (msg.sender != operator) revert NotOperator();
        uint256 cap = tier == 1 ? normalCap / 4 : normalCap;
        if (
            amount == 0 || amount > cap
                || (tier == 1 && lastWithdrawal != 0 && block.timestamp < lastWithdrawal + THROTTLE_COOLDOWN)
        ) {
            revert WithdrawalLimited();
        }
        lastWithdrawal = block.timestamp;
        if (!asset.transfer(operator, amount)) revert AssetActionFailed();
    }

    function grantAllowance(uint256 amount) external {
        if (msg.sender != operator) revert NotOperator();
        if (tier != 0) revert OperatorLocked();
        if (amount > normalCap) revert WithdrawalLimited();
        if (!asset.approve(watchedSpender, amount)) revert AssetActionFailed();
    }

    function freeze(address reactive, uint64 sourceEpoch, bytes32 evidence)
        external
        onlyServiceProvider
        onlyCallbackSender(reactive)
    {
        if (sourceEpoch == epoch) _escalate(3, evidence);
    }

    function applyVerdict(address reactive, AegisVerdict.Verdict calldata verdict, AegisVerdict.Signature calldata sig)
        external
        onlyServiceProvider
        onlyCallbackSender(reactive)
    {
        if (
            verdict.epoch != epoch || verdict.tier <= tier
                || !AegisVerdict.valid(verdictDomain, verdict, sig, verdictSigner)
        ) return;
        _escalate(verdict.tier, verdict.anomalyId);
    }

    function rearm() external {
        if (msg.sender != coldOwner) revert NotColdOwner();
        if (!asset.approve(watchedSpender, 0)) revert AssetActionFailed();
        epoch++;
        tier = 0;
        rebalanced = false;
        lastWithdrawal = 0;
        emit Rearmed(epoch);
    }

    function retryProtection() external {
        if (msg.sender != coldOwner) revert NotColdOwner();
        if (tier != 0) _protect();
    }

    function revokeAllowance() external {
        if (msg.sender != address(this)) revert InternalActionOnly();
        if (!asset.approve(watchedSpender, 0)) revert AssetActionFailed();
    }

    function moveToReserve(bool sweep) external {
        if (msg.sender != address(this)) revert InternalActionOnly();
        uint256 balance = asset.balanceOf(address(this));
        uint256 amount = sweep ? balance : balance / 4;
        if (amount != 0 && !asset.transfer(reserve, amount)) revert AssetActionFailed();
    }

    function coverDebt() external {
        _coverDebt();
    }

    function _escalate(uint8 nextTier, bytes32 evidence) private {
        if (nextTier <= tier) return;
        tier = nextTier;
        emit TierChanged(epoch, nextTier, evidence);
        _protect();
    }

    function _protect() private {
        try this.revokeAllowance{gas: 120_000}() {}
        catch {
            emit ProtectionActionFailed(epoch, tier, this.revokeAllowance.selector);
        }
        if (tier == 3 || (tier == 2 && !rebalanced)) {
            try this.moveToReserve{gas: 160_000}(tier == 3) {
                if (tier == 2) rebalanced = true;
            } catch {
                emit ProtectionActionFailed(epoch, tier, this.moveToReserve.selector);
            }
        }
    }
}
