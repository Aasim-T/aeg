// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {AegisVault} from "../src/AegisVault.sol";
import {AegisReactive} from "../src/AegisReactive.sol";
import {AegisVerdict} from "../src/AegisVerdict.sol";
import {AegisVerdictRegistry} from "../src/AegisVerdictRegistry.sol";
import {DemoAsset, DemoPool} from "../src/AegisDemoSignals.sol";
import {IReactive} from "@reactive/src/interfaces/IReactive.sol";
import {ISystemContract} from "@reactive/src/interfaces/ISystemContract.sol";
import {IPayable} from "@reactive/src/interfaces/IPayable.sol";

interface Vm {
    struct Log {
        bytes32[] topics;
        bytes data;
        address emitter;
    }

    function etch(address target, bytes calldata code) external;
    function prank(address sender) external;
    function expectRevert(bytes4 selector) external;
    function chainId(uint256 newChainId) external;
    function warp(uint256 timestamp) external;
    function recordLogs() external;
    function mockCallRevert(address target, bytes calldata data, bytes calldata revertData) external;
    function clearMockedCalls() external;
    function getRecordedLogs() external returns (Log[] memory);
    function addr(uint256 key) external returns (address);
    function sign(uint256 key, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
}

contract MockSystem {
    uint256 public subscriptions;
    uint256 public requests;
    address public requester;
    address public recipient;
    bytes public payload;

    function subscribe(uint256, address, uint256, uint256, uint256, uint256) external {
        subscriptions++;
    }

    function requestCallbackV_1_0(ISystemContract.CallbackConfiguration_V_1_0 calldata config) external {
        require(config.chainId == 84532 && config.gasLimit == 1_000_000);
        requests++;
        requester = msg.sender;
        recipient = config.recipient;
        payload = config.payload;
    }
}

contract MockProxy {
    function deliver(MockSystem system) external {
        bytes memory payload = system.payload();
        address requester = system.requester();
        assembly {
            mstore(add(payload, 36), requester)
        }
        (bool delivered,) = system.recipient().call(payload);
        require(delivered, "Callback failed");
    }
}

contract EmergencyLoopTest {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address private constant SYSTEM = 0x8888888888888888888888888888888888888888;
    MockSystem private constant system = MockSystem(SYSTEM);
    address private constant HOT = address(0x1234);
    address private constant COLD = address(0x5678);
    address private constant WATCHED = address(0x9876);
    uint256 private constant SIGNER_KEY = 8371;
    uint256 private constant BASE = 84532;
    uint256 private constant UNICHAIN = 1301;
    DemoAsset private asset;
    DemoPool private pool;
    MockProxy private proxy;
    AegisVault private vault;
    AegisReactive private reactive;
    AegisVerdictRegistry private registry;
    uint256 private logIndex;

    function setUp() public {
        vm.chainId(BASE);
        vm.warp(1_000);
        vm.etch(SYSTEM, address(new MockSystem()).code);
        asset = new DemoAsset();
        pool = new DemoPool();
        proxy = new MockProxy();
        registry = new AegisVerdictRegistry(vm.addr(SIGNER_KEY));
        reactive = new AegisReactive(
            AegisReactive.Configuration(address(asset), address(registry), COLD, WATCHED, address(pool), UNICHAIN, 100)
        );
        vault = new AegisVault(
            IPayable(payable(address(proxy))),
            address(reactive),
            registry,
            AegisVault.Configuration(asset, HOT, COLD, COLD, WATCHED, 100)
        );
        reactive.configureVault(address(vault));
        asset.transfer(address(vault), 1_000);
        require(system.subscriptions() == 5);
    }

    function testBenignMalformedAndDuplicateLogsAreIgnored() public {
        IReactive.LogRecord memory observation = withdrawAndRelay(30);
        vm.prank(SYSTEM);
        reactive.react(observation);
        observation.data = hex"01";
        observation.txHash = 99;
        vm.prank(SYSTEM);
        reactive.react(observation);
        (uint8 score,,,) = reactive.risk();
        require(score == 24 && system.requests() == 0 && vault.tier() == 0);
        vm.warp(block.timestamp + 60);
        (score,,,) = reactive.risk();
        require(score == 0);
    }

    function testCriticalTransferSweepsAndFreezesWithoutAI() public {
        withdrawAndRelay(100);
        require(system.requests() == 1 && system.recipient() == address(vault));
        proxy.deliver(system);
        proxy.deliver(system);
        require(vault.frozen() && asset.balanceOf(COLD) == 900 && asset.balanceOf(address(vault)) == 0);

        vm.expectRevert(AegisVault.Frozen.selector);
        vm.prank(HOT);
        vault.withdraw(1);
    }

    function testSignedTierOneThrottlesAndRevokesAllowance() public {
        approveAndRelay();
        bytes32 anomaly = evidence(withdrawAndRelay(25));
        postAndRelay(anomaly, 1, 1);
        require(system.requests() == 1);
        proxy.deliver(system);
        require(vault.tier() == 1 && asset.allowance(address(vault), WATCHED) == 0);

        vm.expectRevert(AegisVault.WithdrawalLimited.selector);
        vm.prank(HOT);
        vault.withdraw(1);

        vm.warp(block.timestamp + 30);
        vm.expectRevert(AegisVault.WithdrawalLimited.selector);
        vm.prank(HOT);
        vault.withdraw(26);

        vm.prank(HOT);
        vault.withdraw(25);
    }

    function testMarketAndApprovalLeadToTierTwoThenSevereMarketFreezes() public {
        priceAndRelay(100);
        approveAndRelay();
        bytes32 anomaly = evidence(priceAndRelay(90));
        postAndRelay(anomaly, 1, 2);
        proxy.deliver(system);
        proxy.deliver(system);
        vm.prank(COLD);
        vault.retryProtection();
        require(vault.tier() == 2 && asset.balanceOf(COLD) == 250);

        vm.expectRevert(AegisVault.OperatorLocked.selector);
        vm.prank(HOT);
        vault.withdraw(1);

        priceAndRelay(70);
        proxy.deliver(system);
        require(vault.tier() == 3 && asset.balanceOf(COLD) == 1_000);
    }

    function testAIOutOfPolicyAndExpiredVerdictsDoNotRequestCallbacks() public {
        bytes32 first = evidence(withdrawAndRelay(50));
        postAndRelay(first, 1, 2);
        require(system.requests() == 0);

        bytes32 second = evidence(withdrawAndRelay(10));
        (uint8 v, bytes32 r, bytes32 s) = signature(second, 1, 1, uint64(block.timestamp + 120), 42);
        vm.expectRevert(AegisVerdictRegistry.InvalidVerdict.selector);
        registry.post(second, 1, 1, uint64(block.timestamp + 120), r, s, v);

        postAndRelay(second, 1, 1);
        vm.warp(block.timestamp + 120);
        proxy.deliver(system);
        require(vault.tier() == 0);
    }

    function testRearmRejectsOldCallbacksAndOldOriginLogs() public {
        IReactive.LogRecord memory oldLog = withdrawAndRelay(100);
        vm.expectRevert(AegisVault.NotColdOwner.selector);
        vault.rearm();
        vm.recordLogs();
        vm.prank(COLD);
        vault.rearm();
        forwardLog(BASE, 1);

        proxy.deliver(system);
        oldLog.txHash = 999;
        vm.prank(SYSTEM);
        reactive.react(oldLog);
        require(vault.epoch() == 2 && reactive.epoch() == 2 && vault.tier() == 0 && system.requests() == 1);

        withdrawAndRelay(100);
        proxy.deliver(system);
        require(vault.tier() == 3);
    }

    function testEmergencyFreezeSurvivesFailedSweepAndSupportsRetry() public {
        withdrawAndRelay(100);
        vm.mockCallRevert(address(asset), abi.encodeCall(DemoAsset.transfer, (COLD, 900)), hex"");
        proxy.deliver(system);
        require(vault.frozen() && asset.balanceOf(address(vault)) == 900);

        vm.clearMockedCalls();
        vm.prank(COLD);
        vault.retryProtection();
        require(vault.frozen() && asset.balanceOf(COLD) == 900);
    }

    function testSpoofedCallersCannotTriggerFreeze() public {
        IReactive.LogRecord memory log;
        (bool forgedLog,) = address(reactive).call(abi.encodeCall(IReactive.react, (log)));
        (bool forgedProxy,) = address(vault).call(abi.encodeCall(AegisVault.freeze, (address(reactive), 1, bytes32(0))));
        vm.prank(address(proxy));
        (bool forgedSender,) = address(vault).call(abi.encodeCall(AegisVault.freeze, (address(this), 1, bytes32(0))));
        require(!forgedLog && !forgedProxy && !forgedSender && !vault.frozen());
    }

    function withdrawAndRelay(uint256 amount) private returns (IReactive.LogRecord memory) {
        vm.recordLogs();
        vm.prank(HOT);
        vault.withdraw(amount);
        return forwardFirstLog(BASE);
    }

    function approveAndRelay() private {
        vm.recordLogs();
        vm.prank(HOT);
        vault.grantAllowance(30);
        forwardFirstLog(BASE);
    }

    function priceAndRelay(uint112 reserve1) private returns (IReactive.LogRecord memory) {
        vm.recordLogs();
        pool.setReserves(100, reserve1);
        return forwardFirstLog(UNICHAIN);
    }

    function postAndRelay(bytes32 anomaly, uint64 epoch, uint8 tier) private {
        uint64 deadline = uint64(block.timestamp + 120);
        (uint8 v, bytes32 r, bytes32 s) = signature(anomaly, epoch, tier, deadline, SIGNER_KEY);
        vm.recordLogs();
        registry.post(anomaly, epoch, tier, deadline, r, s, v);
        forwardFirstLog(BASE);
    }

    function signature(bytes32 anomaly, uint64 epoch, uint8 tier, uint64 deadline, uint256 key)
        private
        returns (uint8 v, bytes32 r, bytes32 s)
    {
        return vm.sign(key, registry.digest(anomaly, epoch, tier, deadline));
    }

    function forwardFirstLog(uint256 chain) private returns (IReactive.LogRecord memory record) {
        return forwardLog(chain, 0);
    }

    function forwardLog(uint256 chain, uint256 index) private returns (IReactive.LogRecord memory record) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        Vm.Log memory entry = logs[index];
        logIndex++;
        record.chainId = chain;
        record.contractAddress = entry.emitter;
        record.topic0 = uint256(entry.topics[0]);
        if (entry.topics.length > 1) record.topic1 = uint256(entry.topics[1]);
        if (entry.topics.length > 2) record.topic2 = uint256(entry.topics[2]);
        record.data = entry.data;
        record.blockNumber = block.number;
        record.logIndex = logIndex;
        record.txHash = logIndex;
        vm.prank(SYSTEM);
        reactive.react(record);
    }

    function evidence(IReactive.LogRecord memory record) private pure returns (bytes32) {
        return keccak256(abi.encode(record.chainId, record.txHash, record.logIndex));
    }
}

