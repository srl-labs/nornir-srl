from typing import Any, List, Dict, Optional
import difflib
import json
import logging
import errno
import re
import socket
import ssl
import time


from pygnmi.client import gNMIclient

from nornir.core.configuration import Config
from nornir.core.exceptions import ConnectionException

from .helpers import strip_modules, normalize_gnmi_resp
from .interfaces import NetworkInstanceMixin
from .routing import RoutingMixin, _suppress_pygnmi_client_logging
from .layer2 import Layer2Mixin
from .neighbor_discovery import NeighborDiscoveryMixin
from .subscription import GnmiSubscription
from .system import SystemMixin
from .ifstats import InterfaceStatsMixin
from .health import HealthMixin

logger = logging.getLogger(__name__)

CONNECTION_NAME = "srlinux"

#: Set once the first unverified connection has warned, so a fabric of fifty
#: nodes says it once rather than fifty times.
_warned_unverified = False


def _resolve_skip_verify(extras: Dict[str, Any]) -> bool:
    """Whether to connect without authenticating the target's certificate.

    pygnmi with ``skip_verify`` set downloads whatever certificate the target
    presents, trusts it as its own root and overrides the hostname check, so
    nothing about the device is actually verified. That is the only thing a
    containerlab node - self-signed, no CA distributed - can offer, so it stays
    the default. Configuring a trust anchor is taken as meaning it should be
    used, and ``skip_verify`` in the inventory settles it either way.
    """
    configured = extras.pop("skip_verify", None)
    if configured is not None:
        return bool(configured)
    return not extras.get("path_cert")


class NodeUnreachable(ConnectionError):
    """A node that could not be connected to at all: nothing answered, or it refused."""


#: How long a node has to accept a TCP connection on its gNMI port before it
#: is reported as not responding. Without it, an address that drops packets
#: takes the operating system's connect timeout - over two minutes - to fail.
CONNECT_TIMEOUT = 10.0


def _probe(host: Any, port: Any, timeout: float = CONNECT_TIMEOUT) -> None:
    """Raise :class:`NodeUnreachable` unless *host* accepts a connection on *port* in time."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return
    except OSError as exc:
        raise _unreachable(exc, host, port) from exc


def _unreachable(cause: BaseException, host: Any, port: Any) -> Exception:
    """What a failed connection to *host* says it was, in words about the node."""
    where = f"{host} on gNMI port {port}"
    if isinstance(cause, ssl.SSLError):
        return ConnectionError(f"TLS handshake with {where} failed: {cause}")
    if isinstance(cause, ConnectionRefusedError):
        return NodeUnreachable(f"not reachable: {where} refused the connection - is gNMI enabled and listening there?")
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return NodeUnreachable(f"not responding: no answer from {where}")
    if isinstance(cause, socket.gaierror):
        return NodeUnreachable(f"not reachable: {host} does not resolve ({cause.strerror or cause})")
    if isinstance(cause, OSError):
        reason = cause.strerror or str(cause)
        if cause.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
            return NodeUnreachable(f"not reachable: no route to {where} ({reason})")
        if cause.errno == errno.ECONNRESET:
            return NodeUnreachable(f"not responding: {where} closed the connection ({reason})")
        return NodeUnreachable(f"not reachable: {where} ({reason})")
    return NodeUnreachable(f"not reachable: {where} ({cause})")


def _connect_failure(exc: BaseException, host: Any, port: Any) -> Optional[Exception]:
    """What a failed connect really was, where pygnmi says it was something else.

    Without certificate verification pygnmi first fetches the node's
    certificate, and reports any failure to as 'The SSL certificate cannot
    be retrieved' - a node that is down, unreachable or not listening reads
    as a TLS problem. The socket error it wraps says which it was. A real
    TLS failure is left one, in words that say so. ``None`` for anything
    else, which is raised as it came.
    """
    if "certificate cannot be retrieved" not in str(exc.args[0] if exc.args else exc):
        return None
    # pygnmi keeps what went wrong underneath as orig_exc.
    cause = getattr(exc, "orig_exc", None) or exc.__cause__ or exc.__context__
    return _unreachable(cause or exc, host, port)


class GnmiPath:
    RE_PATH_COMPONENT = re.compile(
        r"""
    (?P<pname>[^/[]+)  # gNMI path name
    (\[(?P<key>\w\D+)   # gNMI path key
    =
    (?P<value>[^\]]+)    # gNMI path value
    \])?
    """,
        re.VERBOSE,
    )

    def __init__(self, path: str):
        self.path = path.strip("/")
        self.comp = GnmiPath.RE_PATH_COMPONENT.findall(
            self.path
        )  # list (1 item per path-el) of tuples (pname, [k=v], k, v)
        self.elems = ["".join(e[:2]) for e in self.comp]

    def __str__(self):
        return self.path

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.path}')"

    @property
    def resource(self) -> Dict[str, str]:
        return {
            "resource": self.comp[-1][0],
            "key": self.comp[-1][2],
            "val": self.comp[-1][3],
        }

    @property
    def with_no_prefix(self):
        return GnmiPath("/".join([e.split(":")[-1] for e in self.elems]))

    @property
    def parent(self):
        if len(self.elems) > 0:
            return GnmiPath("/".join(self.elems[:-1]))
        return None


class SrLinux(
    NetworkInstanceMixin,
    RoutingMixin,
    Layer2Mixin,
    NeighborDiscoveryMixin,
    SystemMixin,
    InterfaceStatsMixin,
    HealthMixin,
):
    def open(
        self,
        hostname: Optional[str],
        username: Optional[str],
        password: Optional[str],
        port: Optional[int],
        platform: Optional[str],
        extras: Optional[Dict[str, Any]] = None,
        configuration: Optional[Config] = None,
    ) -> None:
        """
        Open a gNMI connection to a device
        """
        target = (hostname, port)
        extras = dict(extras) if extras else {}
        grpc_options = list(extras.pop("grpc_options", []) or [])
        grpc_options.append(("grpc.max_receive_message_length", -1))
        # When a node goes away - a reboot, or the whole lab being redeployed -
        # the channel reconnects on an exponential backoff that gRPC caps at two
        # minutes by default. Calls fail fast in the meantime, so the node reads
        # as permanently gone for far longer than it is actually down.
        if not any(name == "grpc.max_reconnect_backoff_ms" for name, _ in grpc_options):
            grpc_options.append(("grpc.max_reconnect_backoff_ms", 10_000))
        skip_verify = _resolve_skip_verify(extras)
        if skip_verify:
            global _warned_unverified
            if not _warned_unverified:
                _warned_unverified = True
                logger.warning(
                    "gNMI TLS is not verified: the certificate a node presents is "
                    "trusted as-is, so credentials go to whatever answers on the "
                    "gNMI port. Pass --cert-file <ca.pem> to authenticate the fabric."
                )
        # extras can carry credentials-adjacent settings, so only its keys.
        logger.debug(
            "%s: connecting as %s to gNMI port %s (platform=%s, skip_verify=%s, extras=%s, grpc_options=%s)",
            hostname,
            username,
            port,
            platform,
            skip_verify,
            sorted(extras),
            grpc_options,
        )
        started = time.perf_counter()
        # Whether anything answers at all, quickly and in its own words,
        # before pygnmi takes minutes to call it a certificate problem.
        if hostname and port:
            try:
                _probe(hostname, port)
            except NodeUnreachable as exc:
                logger.debug("%s: connect failed: %s", hostname, exc)
                raise
        _connection = gNMIclient(
            target=target,
            username=username,
            password=password,
            skip_verify=skip_verify,
            grpc_options=grpc_options,
            **extras,  # type: ignore
        )
        try:
            # pygnmi logs its own error before raising, and calls a node that
            # does not answer a certificate problem: said once, and right, below.
            with _suppress_pygnmi_client_logging():
                _connection.connect()
        except Exception as exc:  # noqa: BLE001 - re-raised, clearer where it can be
            clearer = _connect_failure(exc, hostname, port)
            if clearer is None:
                raise
            logger.debug("%s: connect failed: %s (pygnmi: %s)", hostname, clearer, exc)
            raise clearer from exc
        self._connection = _connection
        self.connection = self
        self.hostname = hostname
        self.capabilities = self._connection.capabilities()
        caps = self.capabilities if isinstance(self.capabilities, dict) else {}
        logger.debug(
            "%s: connected in %.3fs, gNMI %s, %d model(s), encodings %s",
            hostname,
            time.perf_counter() - started,
            caps.get("gnmi_version", "?"),
            len(caps.get("supported_models") or []),
            sorted(set(caps.get("supported_encodings") or [])),
        )

    def gnmi_get(self, **kw):
        logger.debug("%s: raw Get %s", self.hostname, kw)
        return self._connection.get(**kw)

    def gnmi_set(self, **kw):
        logger.debug("%s: raw Set %s", self.hostname, kw)
        return self._connection.set(**kw)

    def gnmi_subscribe(self, subscribe: Dict[str, Any]) -> GnmiSubscription:
        """Open a gNMI Subscribe RPC on this connection.

        Returns a subscription that yields parsed telemetry notifications and is
        torn down with ``.close()``. The RPC runs on the same gRPC channel as
        ``get``/``set``, so it stays tied to the lifetime of this connection.
        """
        if not self._connection:
            raise ConnectionException("no active connection")
        logger.debug("%s: opening Subscribe RPC: %s", self.hostname, subscribe)
        return GnmiSubscription(
            self._connection, subscribe, name=self.hostname or "target"
        )

    def close(self) -> None:
        if getattr(self, "_connection", None) is not None:
            logger.debug("%s: closing the gNMI connection", self.hostname)
            try:
                self._connection.close()
            except Exception as exc:  # noqa: BLE001 - best effort teardown
                logger.debug("%s: closing the connection failed: %s", self.hostname, exc)
            self._connection = None

    def __repr__(self) -> str:
        return f"{self.__class__.__name__} on {self.hostname}"

    def get(
        self,
        paths: List[str],
        datatype: Optional[str] = "config",
        strip_mod: Optional[bool] = True,
    ) -> List[Dict[str, Any]]:
        if self._connection:
            clean_paths = [p.replace("[name=*]", "") if "[name=*]" in p else p for p in paths]
            logger.debug(
                "%s: Get %s (datatype=%s)", self.hostname, clean_paths, datatype
            )
            started = time.perf_counter()
            try:
                resp = normalize_gnmi_resp(
                    self._connection.get(
                        path=clean_paths, datatype=datatype, encoding="json_ietf"  # type: ignore
                    )
                )
            except Exception as exc:
                logger.debug(
                    "%s: Get %s failed after %.3fs: %s: %s",
                    self.hostname,
                    clean_paths,
                    time.perf_counter() - started,
                    type(exc).__name__,
                    exc,
                )
                raise
            logger.debug(
                "%s: Get %s answered %d notification(s) in %.3fs",
                self.hostname,
                clean_paths,
                len(resp),
                time.perf_counter() - started,
            )
        else:
            raise ConnectionException("no active connection")
        if strip_mod:
            return [strip_modules(d) for d in resp]
        else:
            return resp

    def set_config(
        self,
        input: List[Dict[str, Any]],
        op: Optional[str] = "update",
        dry_run: Optional[bool] = False,
        strip_mod: Optional[bool] = True,
    ) -> str:
        device_cfg_after = []
        r_list: List[str] = []
        for r in input:
            r_list += r.keys()
        #        r_list = [ list(r.keys())[0] for r in input ]
        logger.debug(
            "%s: %s%s on %s",
            self.hostname,
            "dry-run " if dry_run else "",
            op,
            r_list,
        )
        device_cfg_before = self.get(paths=r_list, datatype="config")

        if not dry_run:
            paths = []
            for d in input:
                for p, v in d.items():
                    ### to check - hack
                    ### to address intents that are lists, e.g. /interface
                    #                    if isinstance(v, list):
                    #                        v = { p: v }
                    #                        p = '/'.join(p.split('/')[:-1])
                    #                        if len(p) == 0:
                    #                            p = "/"
                    ###
                    paths.append((p, v))
            if op == "update":
                r = self._connection.set(update=paths, encoding="json_ietf")
            elif op == "replace":
                r = self._connection.set(replace=paths, encoding="json_ietf")
            elif op == "delete":
                delete_paths = [list(p.keys())[0] for p in input]
                r = self._connection.set(delete=delete_paths, encoding="json_ietf")
            else:
                raise ValueError(f"invalid value for parameter 'op': {op}")
            device_cfg_after = self.get(paths=r_list, datatype="config")
        else:
            device_cfg_after = input

        #        dd = DeepDiff(device_cfg_before, device_cfg_after)
        diff = ""
        for i in range(len(r_list)):
            before_json = json.dumps(device_cfg_before[i], indent=2, sort_keys=True)
            after_json = json.dumps(device_cfg_after[i], indent=2, sort_keys=True)
            for line in difflib.unified_diff(
                before_json.splitlines(keepends=True),
                after_json.splitlines(keepends=True),
                fromfile="before",
                tofile="after",
                n=5,
            ):
                diff += line
            if len(diff) > 0:
                diff += "\n"

        return diff
