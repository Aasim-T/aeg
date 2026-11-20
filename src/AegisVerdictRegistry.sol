// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {AegisVerdict} from "./AegisVerdict.sol";

contract AegisVerdictRegistry {
    error InvalidVerdict();

    event AIRiskVerdictPosted(
        bytes32 indexed anomalyId, uint64 epoch, uint8 tier, uint64 deadline, bytes32 r, bytes32 s, uint8 v
    );

    address public immutable signer;
    bytes32 public immutable domainSeparator;
    mapping(bytes32 => bool) public posted;

    constructor(address signer_) {
        if (signer_ == address(0)) revert InvalidVerdict();
        signer = signer_;
        domainSeparator = AegisVerdict.domain(block.chainid, address(this));
    }

    function digest(bytes32 anomalyId, uint64 epoch, uint8 tier, uint64 deadline) external view returns (bytes32) {
        return AegisVerdict.digest(domainSeparator, AegisVerdict.Verdict(anomalyId, epoch, tier, deadline));
    }

    function post(bytes32 anomalyId, uint64 epoch, uint8 tier, uint64 deadline, bytes32 r, bytes32 s, uint8 v)
        external
    {
        AegisVerdict.Verdict memory verdict = AegisVerdict.Verdict(anomalyId, epoch, tier, deadline);
        if (posted[anomalyId] || !AegisVerdict.valid(domainSeparator, verdict, AegisVerdict.Signature(r, s, v), signer))
        {
            revert InvalidVerdict();
        }
        posted[anomalyId] = true;
        emit AIRiskVerdictPosted(anomalyId, epoch, tier, deadline, r, s, v);
    }
}
