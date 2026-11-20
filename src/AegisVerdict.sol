// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

/// @notice Shared EIP-712 format for the registry and the destination vault.
library AegisVerdict {
    bytes32 private constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant VERDICT_TYPEHASH =
        keccak256("RiskVerdict(bytes32 anomalyId,uint64 epoch,uint8 tier,uint64 deadline)");
    uint256 private constant HALF_CURVE_ORDER = 0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;

    struct Verdict {
        bytes32 anomalyId;
        uint64 epoch;
        uint8 tier;
        uint64 deadline;
    }

    struct Signature {
        bytes32 r;
        bytes32 s;
        uint8 v;
    }

    function domain(uint256 chainId, address registry) internal pure returns (bytes32) {
        return keccak256(abi.encode(DOMAIN_TYPEHASH, keccak256("AEGIS Risk"), keccak256("1"), chainId, registry));
    }

    function digest(bytes32 domainSeparator, Verdict memory verdict) internal pure returns (bytes32) {
        bytes32 message =
            keccak256(abi.encode(VERDICT_TYPEHASH, verdict.anomalyId, verdict.epoch, verdict.tier, verdict.deadline));
        return keccak256(abi.encodePacked(hex"1901", domainSeparator, message));
    }

    function valid(bytes32 domainSeparator, Verdict memory verdict, Signature memory signature, address signer)
        internal
        view
        returns (bool)
    {
        if (
            verdict.anomalyId == bytes32(0) || verdict.epoch == 0 || block.timestamp >= verdict.deadline
                || verdict.tier == 0 || verdict.tier > 2 || uint256(signature.s) > HALF_CURVE_ORDER
                || (signature.v != 27 && signature.v != 28)
        ) return false;
        return ecrecover(digest(domainSeparator, verdict), signature.v, signature.r, signature.s) == signer;
    }
}
