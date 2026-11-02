"""Linearizable etcd v3 transactions and leases for the optional cluster control plane."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode()


@dataclass(frozen=True)
class Record:
    value: dict
    revision: int


class Unavailable(RuntimeError):
    """No authoritative quorum response; never authorize work from cached state."""


class EtcdStore:
    """Use etcd's JSON gateway; consensus is provided by etcd, not by this client.

    Reads are linearizable. Mutating requests are not retried after an ambiguous
    failure; callers reconcile persisted records before deciding what to do next.
    TLS client certificates and an optional authentication token are supported.
    """

    def __init__(self, endpoints: list[str], prefix: str, *, timeout: float = 3,
                 ca: str | None = None, cert: str | None = None, key: str | None = None,
                 token: str | None = None):
        if not endpoints or any(not e.startswith(("https://", "http://")) for e in endpoints):
            raise ValueError("cluster endpoints must be HTTP(S) etcd client URLs")
        self.endpoints = [e.rstrip("/") for e in endpoints]
        self.prefix = prefix.rstrip("/") + "/"
        self.timeout = timeout
        self.token = token
        self.context = ssl.create_default_context(cafile=ca)
        if cert:
            self.context.load_cert_chain(cert, key)
        self.cluster_id = None

    def rpc(self, path: str, payload: dict, *, read: bool = False, stream: bool = False) -> dict:
        data = json.dumps(payload).encode()
        last = None
        for endpoint in self.endpoints:
            request = Request(endpoint + path, data=data, headers={"Content-Type": "application/json"})
            if self.token:
                request.add_header("Authorization", self.token)
            try:
                with urlopen(request, timeout=self.timeout, context=self.context) as response:
                    raw = response.readline(16 * 1024 * 1024 + 1) if stream else response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise Unavailable("etcd response exceeds limit")
                result = json.loads(raw)
                if "error" in result:
                    raise Unavailable("etcd rejected the request")
                result = result.get("result", result)
                cluster_id = result.get("header", {}).get("cluster_id")
                if cluster_id:
                    if self.cluster_id is not None and self.cluster_id != cluster_id:
                        raise Unavailable("etcd endpoints belong to different clusters")
                    self.cluster_id = cluster_id
                self.endpoints = [endpoint, *(item for item in self.endpoints if item != endpoint)]
                return result
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                last = error
                if not read:
                    break
        raise Unavailable(f"etcd quorum unavailable ({type(last).__name__}); reconcile before retrying")

    def _key(self, key: str) -> str:
        return _encoded((self.prefix + key).encode())

    def get(self, key: str) -> Record | None:
        rows = self.rpc("/v3/kv/range", {"key": self._key(key)}, read=True).get("kvs", [])
        return self._record(rows[0]) if rows else None

    @staticmethod
    def _record(row: dict) -> Record:
        return Record(json.loads(base64.b64decode(row["value"])), int(row["mod_revision"]))

    def scan(self, prefix: str) -> dict[str, Record]:
        start = (self.prefix + prefix).encode()
        end = start[:-1] + bytes([start[-1] + 1])
        rows = self.rpc("/v3/kv/range", {"key": _encoded(start), "range_end": _encoded(end)}, read=True).get("kvs", [])
        return {base64.b64decode(row["key"]).decode()[len(self.prefix):]: self._record(row) for row in rows}

    def txn(self, checks: dict[str, Record | None], puts: dict[str, dict], *,
            deletes: tuple[str, ...] = (), leases: dict[str, int] | None = None) -> bool:
        """CAS records and assignments together; a stale leader comparison must fail."""
        compare = [{"key": self._key(key), "target": "MOD", "result": "EQUAL",
                    "mod_revision": str(record.revision if record else 0)} for key, record in checks.items()]
        operations = []
        for key, value in puts.items():
            data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
            if len(data) > 1024 * 1024:
                raise ValueError("cluster record exceeds 1 MiB; reduce task metadata")
            request = {"key": self._key(key), "value": _encoded(data)}
            if leases and key in leases:
                request["lease"] = str(leases[key])
            operations.append({"request_put": request})
        operations += [{"request_delete_range": {"key": self._key(key)}} for key in deletes]
        return bool(self.rpc("/v3/kv/txn", {"compare": compare, "success": operations}).get("succeeded"))

    def grant(self, ttl: int) -> int:
        return int(self.rpc("/v3/lease/grant", {"TTL": ttl})["ID"])

    def keepalive(self, lease: int) -> None:
        result = self.rpc("/v3/lease/keepalive", {"ID": str(lease)}, stream=True)
        if int(result.get("TTL", 0)) <= 0:
            raise Unavailable("cluster session lease expired")

    def members(self) -> int:
        return sum(not row.get("isLearner", False) for row in
                   self.rpc("/v3/cluster/member/list", {}, read=True).get("members", []))
