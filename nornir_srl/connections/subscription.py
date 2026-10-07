"""A gNMI ``Subscribe`` RPC whose teardown releases the session on the target.

pygnmi's own subscriber (``gNMIclient.subscribe2``) ends a subscription by
letting its request iterator return, which half-closes only the client side of
the stream. The target keeps the RPC - and the gRPC session behind it - alive,
so every re-subscription burns a session for the remaining lifetime of the
process. SR Linux permits 20 concurrent sessions per gRPC server by default
(``/system/grpc-server[name=mgmt]/session-limit``, shared with every other gRPC
client of the node), so a long-running server exhausts a node after a couple of
dozen re-subscriptions and every further RPC is rejected with
``Max number of concurrent sessions reached``.

Keeping a reference to the grpc call lets :meth:`GnmiSubscription.close` cancel
the RPC, which tears down the session on the target straight away.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, Optional

import grpc
from pygnmi.client import telemetryParser
from pygnmi.spec.v080.gnmi_pb2_grpc import gNMIStub

logger = logging.getLogger(__name__)

#: Queued by :meth:`GnmiSubscription.close` to wake a blocked reader at once.
_CLOSED = object()


class SubscriptionClosed(Exception):
    """Raised by :meth:`GnmiSubscription.get_update` after a local close."""


class GnmiSubscription:
    """One ``Subscribe`` RPC, drained by a background thread.

    The interface mirrors the part of pygnmi's ``StreamSubscriber`` that callers
    use - :meth:`get_update`, :attr:`error` and :meth:`close` - so it is a drop-in
    replacement, except that closing it actually cancels the RPC.
    """

    def __init__(
        self,
        client: Any,
        subscribe: Dict[str, Any],
        *,
        name: str = "",
    ) -> None:
        channel, metadata = _rpc_handles(client)
        # _build_subscriptionrequest fills in defaults on the dict it is given.
        request = client._build_subscriptionrequest(dict(subscribe))

        self._updates: "queue.Queue[Any]" = queue.Queue()
        self._closing = threading.Event()
        self.error: Optional[BaseException] = None
        self._name = name or "target"
        #: Notifications received, logged on close to date a silent stream.
        self._received = 0

        self._call = gNMIStub(channel).Subscribe(iter((request,)), metadata=metadata)
        self._reader = threading.Thread(
            target=self._drain,
            name=f"gnmi-rx-{name or 'target'}",
            daemon=True,
        )
        self._reader.start()

    def _drain(self) -> None:
        try:
            for message in self._call:
                self._received += 1
                if self._received == 1:
                    logger.debug("%s: first notification received", self._name)
                self._updates.put(message)
        except grpc.RpcError as exc:
            if not self._closing.is_set():
                self.error = exc
            logger.debug(
                "%s: subscription stream ended after %d notification(s): %s",
                self._name,
                self._received,
                exc,
            )
            return
        except Exception as exc:  # noqa: BLE001 - surfaced through .error
            self.error = exc
            logger.debug(
                "%s: subscription reader failed after %d notification(s): %s",
                self._name,
                self._received,
                exc,
            )
            return
        if not self._closing.is_set():
            # A stream subscription is not supposed to end on its own; treat it
            # as a failure so the caller reconnects instead of polling an empty
            # queue forever.
            self.error = ConnectionError("subscription closed by target")

    @property
    def backlog(self) -> int:
        """Notifications received and not yet read."""
        return self._updates.qsize()

    def get_update(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Return the next parsed notification.

        Raises :class:`TimeoutError` if none arrives within *timeout*, or
        :class:`SubscriptionClosed` if the subscription is closed while waiting.
        """
        try:
            message = self._updates.get(block=True, timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no update from target after {timeout}s") from None
        if message is _CLOSED:
            raise SubscriptionClosed("subscription was closed")
        return telemetryParser(message)

    def close(self) -> None:
        """Cancel the RPC, freeing the session on the target."""
        self._closing.set()
        logger.debug(
            "%s: cancelling subscription after %d notification(s)",
            self._name,
            self._received,
        )
        try:
            self._call.cancel()
        except Exception as exc:  # noqa: BLE001 - best effort teardown
            logger.debug("cancelling subscription failed: %s", exc)
        # Cancelling ends the drain thread but says nothing to a consumer already
        # blocked on the queue, which would otherwise sit out its full timeout
        # before noticing - once per node, on every re-subscribe and shutdown.
        self._updates.put(_CLOSED)
        self._reader.join(timeout=0.2)


def _rpc_handles(client: Any) -> Any:
    """Return the ``(channel, metadata)`` of a connected pygnmi client.

    Both are private to ``gNMIclient`` and only reachable through their mangled
    names; pygnmi offers no way to issue a Subscribe on an existing channel
    while retaining the call object.
    """
    channel = getattr(client, "_gNMIclient__channel", None)
    metadata = getattr(client, "_gNMIclient__metadata", None)
    if channel is None or metadata is None:
        raise ConnectionError(
            "pygnmi client exposes no gRPC channel; it is either not connected "
            "or its internals changed"
        )
    return channel, metadata
