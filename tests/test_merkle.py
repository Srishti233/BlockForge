import hashlib

import pytest

from blockforge.blockchain import merkle


def tid(i: int) -> str:
    return hashlib.sha256(b"tx%d" % i).hexdigest()


def h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def test_known_vector_two_leaves():
    a, b = tid(1), tid(2)
    la, lb = h(b"\x00" + bytes.fromhex(a)), h(b"\x00" + bytes.fromhex(b))
    assert merkle.merkle_root([a, b]) == h(b"\x01" + la + lb).hex()


def test_known_vector_three_leaves_duplicates_last():
    ids = [tid(i) for i in range(3)]
    l = [h(b"\x00" + bytes.fromhex(x)) for x in ids]
    n01, n22 = h(b"\x01" + l[0] + l[1]), h(b"\x01" + l[2] + l[2])
    assert merkle.merkle_root(ids) == h(b"\x01" + n01 + n22).hex()


def test_single_leaf_and_empty():
    assert merkle.merkle_root([tid(1)]) == merkle.leaf_hash(tid(1)).hex()
    assert merkle.merkle_proof([tid(1)], 0) == []
    assert merkle.verify_proof(tid(1), [], merkle.merkle_root([tid(1)]))
    assert merkle.merkle_root([]) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8, 9, 15, 16, 17, 33])
def test_every_leaf_has_a_valid_proof(n):
    ids = [tid(i) for i in range(n)]
    root = merkle.merkle_root(ids)
    for i, t in enumerate(ids):
        proof = merkle.merkle_proof(ids, i)
        assert merkle.verify_proof(t, proof, root), (n, i)


def test_tampered_proofs_fail():
    ids = [tid(i) for i in range(6)]
    root = merkle.merkle_root(ids)
    proof = merkle.merkle_proof(ids, 2)
    assert not merkle.verify_proof(tid(99), proof, root)                  # wrong leaf
    bad = [dict(p) for p in proof]
    bad[0]["hash"] = "00" * 32
    assert not merkle.verify_proof(ids[2], bad, root)                     # wrong sibling
    flipped = [dict(p) for p in proof]
    flipped[0]["side"] = "left" if flipped[0]["side"] == "right" else "right"
    assert not merkle.verify_proof(ids[2], flipped, root)                 # wrong side
    assert not merkle.verify_proof(ids[2], proof, "00" * 32)              # wrong root
    assert not merkle.verify_proof(ids[2], [{"hash": "zz", "side": "left"}], root)
    assert not merkle.verify_proof(ids[2], [{"hash": "00" * 32, "side": "up"}], root)
    assert not merkle.verify_proof(ids[2], [{"nothash": 1}], root)
    assert not merkle.verify_proof("nothex", proof, root)


def test_leaf_and_internal_domains_differ():
    # a 2-leaf root must not equal the leaf hash of the concatenation (second-preimage guard)
    a, b = tid(1), tid(2)
    la, lb = merkle.leaf_hash(a), merkle.leaf_hash(b)
    assert merkle.merkle_root([a, b]) != h(b"\x00" + la + lb).hex()


def test_proof_index_bounds():
    with pytest.raises(IndexError):
        merkle.merkle_proof([tid(1)], 1)
