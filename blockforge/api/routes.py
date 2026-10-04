"""FastAPI application: public REST API (/api/v1), internal P2P endpoints (/p2p), explorer."""
from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from blockforge import __version__
from blockforge.api.schemas import (
    AddressModel,
    BlockMessage,
    HandshakeModel,
    MerkleVerifyModel,
    PartitionModel,
    PeerAddModel,
    TransactionModel,
    TxMessage,
)
from blockforge.blockchain import merkle
from blockforge.blockchain.block import Block
from blockforge.blockchain.transaction import MalformedError, Transaction
from blockforge.network.node import Node
from blockforge.network.protocol import ProtocolError, valid_peer_addr

log = logging.getLogger("blockforge.api")
EXPLORER_DIR = Path(__file__).resolve().parent.parent / "explorer"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class _TooLarge(Exception):
    """Raised internally when a streamed body exceeds the size cap."""


class BodyLimitMiddleware:
    """Pure-ASGI request size cap: checks Content-Length and counts streamed bytes."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await self._reject(send)
                        return
                except ValueError:
                    await self._reject(send)
                    return
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(send)

    async def _reject(self, send) -> None:
        body = json.dumps({"error": {"code": "PAYLOAD_TOO_LARGE",
                                     "message": f"request body exceeds {self.max_bytes} bytes"}}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def block_summary(b: Block) -> dict[str, Any]:
    h = b.header
    return {"hash": b.hash, "height": h.height, "prev_hash": h.prev_hash, "merkle_root": h.merkle_root,
            "state_root": h.state_root, "timestamp": h.timestamp, "difficulty_bits": h.difficulty_bits,
            "nonce": h.nonce, "miner": h.miner, "tx_count": len(b.transactions),
            "size_bytes": b.size_bytes()}


def create_app(node: Node, *, manage_node: bool = True) -> FastAPI:
    """Build the app around a Node. With manage_node the node starts/stops with the app."""
    chain = node.chain

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_node:
            node.start()
        yield
        if manage_node:
            node.stop()

    app = FastAPI(title="BlockForge", version=__version__, lifespan=lifespan,
                  description="Educational blockchain node. Not for real money.",
                  docs_url=None, redoc_url=None, openapi_url="/openapi.json")
    app.add_middleware(BodyLimitMiddleware, max_bytes=node.cfg.max_request_bytes)

    # ------------------------------------------------------------ cross-cutting
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return _error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        # Deliberately omit the submitted values: they could contain secrets.
        parts = [f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
                 for e in exc.errors()[:5]]
        return _error_response(422, "VALIDATION_ERROR", "; ".join(parts) or "invalid request")

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return _error_response(500, "INTERNAL", "internal server error")

    def rate_guard(request: Request) -> None:
        key = request.client.host if request.client else "unknown"
        if not node.limiter.allow(key):
            raise ApiError(429, "RATE_LIMITED", "too many requests")

    def not_found(what: str) -> ApiError:
        return ApiError(404, "NOT_FOUND", f"{what} not found")

    # ------------------------------------------------------------ explorer + docs
    @app.get("/", include_in_schema=False)
    def explorer_index():
        return FileResponse(EXPLORER_DIR / "index.html", media_type="text/html")

    @app.get("/docs", include_in_schema=False)
    def docs_page():
        return FileResponse(EXPLORER_DIR / "docs.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=str(EXPLORER_DIR)), name="static")

    # ------------------------------------------------------------ public API
    api = "/api/v1"

    @app.get(f"{api}/node", tags=["node"])
    def get_node():
        return node.info()

    @app.get(f"{api}/chain/info", tags=["chain"])
    def chain_info():
        return {**chain.info(), "node_id": node.node_id}

    @app.get(f"{api}/chain/validate", tags=["chain"])
    def chain_validate():
        return chain.validate()

    @app.get(f"{api}/chain/forks", tags=["chain"])
    def chain_forks():
        return {"forks": list(chain.fork_events)}

    @app.get(f"{api}/blocks", tags=["blocks"])
    def list_blocks(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return {"total": chain.height() + 1, "limit": limit, "offset": offset,
                "blocks": [block_summary(b) for b in chain.list_blocks(limit, offset)]}

    @app.get(f"{api}/blocks/{{height_or_hash}}", tags=["blocks"])
    def get_block(height_or_hash: str):
        if not (height_or_hash.isdigit() and len(height_or_hash) < 12) and not _HEX64.match(height_or_hash):
            raise ApiError(400, "BAD_REQUEST", "expected a block height or a 64-hex block hash")
        block = chain.get_block(height_or_hash)
        if block is None:
            raise not_found("block")
        is_main = chain.is_main(block.hash)
        out = block_summary(block)
        out.update({"transactions": [t.to_dict() for t in block.transactions], "is_main": is_main,
                    "confirmations": chain.height() - block.height + 1 if is_main else 0})
        return out

    @app.get(f"{api}/transactions/{{tx_id}}", tags=["transactions"])
    def get_transaction(tx_id: str):
        if not _HEX64.match(tx_id):
            raise ApiError(400, "BAD_REQUEST", "tx id must be 64 lowercase hex characters")
        found = chain.find_tx(tx_id)
        if found is None:
            raise not_found("transaction")
        return found

    @app.post(f"{api}/transactions", status_code=202, tags=["transactions"],
              dependencies=[Depends(rate_guard)])
    def post_transaction(body: TransactionModel):
        """Submit an already-signed transaction. Signing is client-side only."""
        try:
            tx = Transaction.from_dict(body.model_dump())
        except MalformedError as exc:
            raise ApiError(400, "MALFORMED", str(exc)) from exc
        err = node.submit_transaction(tx)
        if err is not None:
            raise ApiError(409 if err.code == "DUPLICATE" else 400, err.code, err.message)
        return {"tx_id": tx.tx_id, "status": "accepted"}

    @app.get(f"{api}/mempool", tags=["transactions"])
    def get_mempool():
        txs = chain.mempool.all()
        return {"count": len(txs), "transactions": [t.to_dict() for t in txs]}

    @app.get(f"{api}/accounts/{{address}}", tags=["accounts"])
    def get_account(address: str):
        if not re.match(r"^bf[0-9a-f]{40}$", address):
            raise ApiError(400, "BAD_REQUEST", "address must be 'bf' + 40 lowercase hex characters")
        return chain.account(address)

    @app.post(f"{api}/mine", tags=["mining"], dependencies=[Depends(rate_guard)])
    def mine(body: AddressModel):
        """Mine exactly one block, paying the reward to `address`."""
        try:
            block = node.mine_once(body.address)
        except RuntimeError as exc:
            raise ApiError(409, "STALE_TIP", str(exc)) from exc
        return block_summary(block)

    @app.post(f"{api}/miner/start", tags=["mining"], dependencies=[Depends(rate_guard)])
    def miner_start(body: AddressModel):
        node.miner.start(body.address)
        return {"mining": True, "address": body.address}

    @app.post(f"{api}/miner/stop", tags=["mining"], dependencies=[Depends(rate_guard)])
    def miner_stop():
        node.miner.stop()
        return {"mining": False}

    @app.get(f"{api}/peers", tags=["network"])
    def get_peers():
        return {"peers": [p.to_dict() for p in node.peers.all()], "bans": node.peers.bans()}

    @app.post(f"{api}/peers", tags=["network"], dependencies=[Depends(rate_guard)])
    def add_peer(body: PeerAddModel):
        if not valid_peer_addr(body.address):
            raise ApiError(400, "BAD_REQUEST", "address must look like host:port")
        ok = node.connect_peer(body.address)
        if not ok:
            raise ApiError(502, "PEER_UNREACHABLE", "could not complete a handshake with that peer")
        return {"connected": True, "address": body.address}

    @app.get(f"{api}/merkle/proof/{{tx_id}}", tags=["merkle"])
    def merkle_proof(tx_id: str):
        if not _HEX64.match(tx_id):
            raise ApiError(400, "BAD_REQUEST", "tx id must be 64 lowercase hex characters")
        found = chain.find_tx(tx_id)
        if found is None or found["status"] != "confirmed":
            raise not_found("confirmed transaction")
        return {"tx_id": tx_id, "block_hash": found["block_hash"], "block_height": found["block_height"],
                "merkle_root": found["merkle_root"], "proof": found["merkle_proof"]}

    @app.post(f"{api}/merkle/verify", tags=["merkle"], dependencies=[Depends(rate_guard)])
    def merkle_verify(body: MerkleVerifyModel):
        steps = [s.model_dump() for s in body.proof]
        return {"valid": merkle.verify_proof(body.tx_id, steps, body.root)}

    # ------------------------------------------------------------ internal P2P
    def proto(call, *args):
        try:
            return call(*args)
        except ProtocolError as exc:
            status = 403 if exc.code == "BANNED" else 400
            raise ApiError(status, exc.code, exc.message) from exc

    @app.post("/p2p/handshake", tags=["p2p"], dependencies=[Depends(rate_guard)])
    def p2p_handshake(body: HandshakeModel):
        return proto(node.handle_handshake, body.model_dump())

    @app.get("/p2p/peers", tags=["p2p"])
    def p2p_peers():
        return {"peers": node.peer_list()}

    @app.get("/p2p/status", tags=["p2p"])
    def p2p_status():
        return node.status()

    @app.post("/p2p/tx", tags=["p2p"], dependencies=[Depends(rate_guard)])
    def p2p_tx(body: TxMessage):
        return proto(node.handle_tx, body.tx.model_dump(), body.sender)

    @app.post("/p2p/block", tags=["p2p"], dependencies=[Depends(rate_guard)])
    def p2p_block(body: BlockMessage):
        return proto(node.handle_block, body.block.model_dump(), body.sender)

    @app.get("/p2p/blocks", tags=["p2p"], dependencies=[Depends(rate_guard)])
    def p2p_blocks(start: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return {"blocks": node.serve_blocks(start, limit)}

    @app.get("/p2p/block/{block_hash}", tags=["p2p"], dependencies=[Depends(rate_guard)])
    def p2p_block_by_hash(block_hash: str):
        if not _HEX64.match(block_hash):
            raise ApiError(400, "BAD_REQUEST", "block hash must be 64 lowercase hex characters")
        data = node.serve_block(block_hash)
        if data is None:
            raise not_found("block")
        return data

    if node.cfg.test_hooks:   # opt-in, demo/test only: simulate a network split between real processes
        @app.post("/p2p/partition", tags=["p2p"])
        def p2p_partition(body: PartitionModel):
            node.partition(body.addrs)
            return {"partitioned": sorted(node._partitioned)}

        @app.post("/p2p/heal", tags=["p2p"])
        def p2p_heal():
            node.heal()
            return {"partitioned": []}

    return app
