// SPDX-License-Identifier: MIT
pragma solidity 0.8.29;

import {IAegisAsset} from "./AegisVault.sol";

/// @notice Fixed-supply test token for reproducible testnet signals.
contract DemoAsset is IAegisAsset {
    string public constant name = "AEGIS Test Asset";
    string public constant symbol = "AGTEST";
    uint8 public constant decimals = 18;
    uint256 public constant totalSupply = 1_000_000 ether;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor() {
        balanceOf[msg.sender] = totalSupply;
        emit Transfer(address(0), msg.sender, totalSupply);
    }

    function transfer(address recipient, uint256 amount) external returns (bool) {
        _transfer(msg.sender, recipient, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address recipient, uint256 amount) external returns (bool) {
        allowance[from][msg.sender] -= amount;
        _transfer(from, recipient, amount);
        return true;
    }

    function _transfer(address from, address recipient, uint256 amount) private {
        require(recipient != address(0));
        balanceOf[from] -= amount;
        balanceOf[recipient] += amount;
        emit Transfer(from, recipient, amount);
    }
}

/// @notice Emits the same reserve event as a V2 pool, with demo-controlled values.
contract DemoPool {
    address public immutable operator = msg.sender;
    event Sync(uint112 reserve0, uint112 reserve1);

    function setReserves(uint112 reserve0, uint112 reserve1) external {
        require(msg.sender == operator);
        emit Sync(reserve0, reserve1);
    }
}
