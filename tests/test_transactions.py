import dataclasses

import pytest

from blockforge.blockchain.mempool import Mempool
from blockforge.blockchain.state import WorldState
from blockforge.blockchain.transaction import (
    MalformedError, Transaction, validate_transaction_stateless)
from blockforge.blockchain.wallet import Wallet

CID = "test-chain"


def mk(w, to, amount=5, fee=1, nonce=0, chain=CID):
    return w.create_transaction(to.address, amount, fee, nonce, chain, timestamp=1_700_000_100)


def code(tx, chain=CID):
    e = validate_transaction_stateless(tx, chain)
    return e.code if e else None


def test_valid_transaction():
    a, b = Wallet.generate(), Wallet.generate()
    tx = mk(a, b)
    assert code(tx) is None
    assert tx.tx_id == tx.compute_id()
    assert Transaction.from_dict(tx.to_dict()) == tx


def test_bad_signature():
    a, b = Wallet.generate(), Wallet.generate()
    tx = mk(a, b)
    forged = dataclasses.replace(tx, signature=mk(a, b, amount=6).signature)
    assert code(forged) == "INVALID_SIGNATURE"


def test_modified_body_changes_id():
    a, b = Wallet.generate(), Wallet.generate()
    tx = dataclasses.replace(mk(a, b), amount=999)
    assert code(tx) == "MALFORMED"  # tx_id no longer matches


def test_wrong_ownership():
    a, b, c = (Wallet.generate() for _ in range(3))
    tx = mk(a, b)
    stolen = Transaction.create_signed(
        c, recipient=b.address, amount=5, fee=1, nonce=0, chain_id=CID, timestamp=1)
    claiming_a = dataclasses.replace(stolen, sender=a.address)
    claiming_a = dataclasses.replace(claiming_a, tx_id=claiming_a.compute_id())
    assert code(claiming_a) == "BAD_OWNERSHIP"
    assert code(tx) is None


def test_wrong_chain_id():
    a, b = Wallet.generate(), Wallet.generate()
    assert code(mk(a, b, chain="other-chain")) == "WRONG_CHAIN"


@pytest.mark.parametrize("amount,fee", [(0, 1), (-5, 1), (5, -1), (2 ** 63, 0), (2 ** 62, 2 ** 62)])
def test_bad_amounts(amount, fee):
    a, b = Wallet.generate(), Wallet.generate()
    assert code(mk(a, b, amount=amount, fee=fee)) == "BAD_AMOUNT"


def test_malformed_dicts():
    a, b = Wallet.generate(), Wallet.generate()
    good = mk(a, b).to_dict()
    for mutate in [
        lambda d: d.pop("signature"), lambda d: d.update(amount="5"), lambda d: d.update(amount=5.0),
        lambda d: d.update(amount=True), lambda d: d.update(extra=1), lambda d: d.update(sender=5),
    ]:
        d = dict(good)
        mutate(d)
        with pytest.raises(MalformedError):
            Transaction.from_dict(d)
    with pytest.raises(MalformedError):
        Transaction.from_dict("nope")


def test_bad_addresses_and_pubkey():
    a, b = Wallet.generate(), Wallet.generate()
    tx = mk(a, b)
    bad_to = dataclasses.replace(tx, recipient="bf123")
    assert code(bad_to) == "MALFORMED"
    bad_pk = dataclasses.replace(tx, public_key="zz")
    assert code(bad_pk) == "MALFORMED"
    cb = Transaction.coinbase(a.address, 50, 1, 1, CID)
    assert code(cb) == "MALFORMED"


def test_mempool_rules():
    a, b = Wallet.generate(), Wallet.generate()
    state = WorldState({a.address: (100, 0)})
    pool = Mempool(CID, max_size=3)
    t0 = mk(a, b, 10, 1, 0)
    assert pool.add(t0, state) is None
    assert pool.add(t0, state).code == "DUPLICATE"
    assert pool.add(mk(a, b, 11, 1, 0), state).code == "BAD_NONCE"      # double spend of nonce 0
    assert pool.add(mk(a, b, 10, 1, 2), state).code == "BAD_NONCE"      # gap
    assert pool.add(mk(a, b, 500, 1, 1), state).code == "INSUFFICIENT_BALANCE"
    t1 = mk(a, b, 10, 5, 1)
    assert pool.add(t1, state) is None
    assert pool.add(mk(Wallet.generate(), b), state).code == "INSUFFICIENT_BALANCE"
    state2 = WorldState({a.address: (100, 3)})
    assert Mempool(CID).add(t0, state2).code == "BAD_NONCE"              # already used
    # selection respects nonce order even though t1 pays the higher fee
    assert [t.nonce for t in pool.select(10, state)] == [0, 1]
    assert [t.nonce for t in pool.select(1, state)] == [0]
    pool.remove_included([t0.tx_id])
    assert t0.tx_id not in pool and len(pool) == 1
    assert pool.get(t1.tx_id) == t1


def test_mempool_fee_priority_and_cap():
    senders = [Wallet.generate() for _ in range(4)]
    to = Wallet.generate()
    state = WorldState({s.address: (100, 0) for s in senders})
    pool = Mempool(CID, max_size=3)
    for s, fee in zip(senders[:3], (1, 9, 4)):
        assert pool.add(mk(s, to, 5, fee, 0), state) is None
    assert [t.fee for t in pool.select(10, state)] == [9, 4, 1]
    assert pool.add(mk(senders[3], to, 5, 1, 0), state).code == "MEMPOOL_FULL"
    assert pool.add(mk(senders[3], to, 5, 7, 0), state) is None   # evicts the fee-1 tx
    assert sorted(t.fee for t in pool.all()) == [4, 7, 9]


def test_mempool_revalidate_drops_stale():
    a, b = Wallet.generate(), Wallet.generate()
    pool = Mempool(CID)
    t0 = mk(a, b, 10, 1, 0)
    assert pool.add(t0, WorldState({a.address: (100, 0)})) is None
    removed = pool.revalidate(WorldState({a.address: (100, 1)}))
    assert removed == [t0.tx_id] and len(pool) == 0
