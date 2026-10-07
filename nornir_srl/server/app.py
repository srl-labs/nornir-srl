"""Starlette application serving the fcli live report tables."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Sequence

import anyio
from nornir.core import Nornir
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..changes import as_row, parse_since
from ..diff import diff_nodes, diff_tables
from ..history import DEFAULT_RETENTION_DAYS, HistoryError, HistoryStore
from ..lenses import LENSES_BY_NAME, coerce_lens_params, lenses_for
from ..reports import SERVER, ReportSpec, coerce_params, get_report, reports_for
from .agent import NO_PROVIDER, ChatService
from .snapshots import SnapshotStore, _slug, comparable
from .store import FabricStore, ReportLoad, full_table_read
from .table import serialize_table, table_digest

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: How often the browser is offered a fresh render of the table.
DEFAULT_REFRESH = 2.0
#: Keep-alive comment interval so proxies do not drop an idle SSE stream.
SSE_HEARTBEAT = 20.0
#: How often a stream says how far its first answer is, while that is still
#: being worked out - a BGP RIB can take a while on a large fabric.
PROGRESS_INTERVAL = 0.5

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

#: Placeholder in index.html for the cache key of the assets it pulls in.
ASSET_TOKEN = "__ASSETS__"


def asset_version() -> str:
    """A token that changes whenever the bundled JS or CSS does.

    The page itself is served ``no-cache`` so the browser revalidates it on
    every load, and the assets it references are fingerprinted with this. Left
    to itself a browser will happily keep serving a page it cached before a
    feature existed, which shows up as a report whose panel is simply missing:
    the report list comes from the API and lists it, the markup it needs does
    not exist, and nothing in the console says why.
    """
    newest = max(
        (STATIC_DIR / name).stat().st_mtime_ns for name in ("app.js", "style.css")
    )
    return f"{__version__}-{newest:x}"


def parse_kv(values: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse a ``k=v,k=v`` query argument into a dict."""
    if not values:
        return None
    parsed: Dict[str, str] = {}
    for part in values.split(","):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            parsed[key.strip()] = value.strip()
    return parsed or None


def parse_nodes(value: Optional[str]) -> Optional[List[str]]:
    """The ``node`` query argument: the nodes a request is only about, or None."""
    names = [n.strip() for n in (value or "").split(",") if n.strip()]
    return names or None


async def table_events(
    store: FabricStore,
    name: str,
    render: Callable[[], Dict[str, Any]],
    interval: float,
    is_disconnected: Callable[[], Awaitable[bool]],
    progress: Optional[Callable[[], Dict[str, Any]]] = None,
) -> AsyncIterator[bytes]:
    """Yield server-sent events of what *render* makes of the state, until the
    client goes away.

    A table is only pushed when it actually changed, so an idle fabric costs
    nothing but the periodic keep-alive comment. *render* is a report's table
    or a lens's answer; *name* is only for the log.

    Until the first table is out, a ``progress`` event every
    :data:`PROGRESS_INTERVAL` says the query is still running: the seconds it
    has taken, and what *progress* knows of how many nodes have answered. A
    slow first answer then reads as work in progress rather than as nothing.
    """
    loop = asyncio.get_running_loop()
    last_digest = ""
    last_sent = 0.0

    def encoded_render():
        table = render()
        return serialize_table(table), bool(table.get("loading"))

    try:
        while not await is_disconnected() and not store.stopping:
            try:
                task = asyncio.ensure_future(anyio.to_thread.run_sync(encoded_render))
                started = loop.time()
                while not last_digest:
                    done, _pending = await asyncio.wait({task}, timeout=PROGRESS_INTERVAL)
                    if done:
                        break
                    if store.stopping or await is_disconnected():
                        return
                    status: Dict[str, Any] = {"elapsed": round(loop.time() - started, 1)}
                    if progress is not None:
                        try:
                            status.update(progress())
                        except Exception:  # noqa: BLE001 - the elapsed time still says it
                            pass
                    yield f"event: progress\ndata: {json.dumps(status)}\n\n".encode()
                encoded, loading = await task
            except (asyncio.CancelledError, GeneratorExit):
                break
            except ValueError as exc:
                # A lens asked something it cannot answer: an ordinary answer
                # for the browser to show, not a failure to log.
                payload = json.dumps({"error": str(exc)})
                yield f"event: error\ndata: {payload}\n\n".encode()
                await asyncio.sleep(interval)
                continue
            except Exception as exc:  # noqa: BLE001 - surfaced in the browser
                if store.stopping:
                    return
                logger.exception("rendering '%s' failed", name)
                payload = json.dumps({"error": str(exc)})
                yield f"event: error\ndata: {payload}\n\n".encode()
                await asyncio.sleep(interval)
                continue
            if store.stopping:
                return
            digest = encoded.digest
            now = loop.time()
            if digest != last_digest:
                last_digest = digest
                last_sent = now
                yield encoded.event
            elif now - last_sent > SSE_HEARTBEAT:
                last_sent = now
                yield b": keep-alive\n\n"
            deadline = loop.time() + (min(interval, PROGRESS_INTERVAL) if loading else interval)
            while loop.time() < deadline:
                if store.stopping or await is_disconnected():
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.sleep(min(0.1, remaining))
                except (asyncio.CancelledError, GeneratorExit):
                    return
    except (asyncio.CancelledError, GeneratorExit):
        return


class SuppressCancelledErrorMiddleware:
    """Catch asyncio.CancelledError during uvicorn shutdown to prevent traceback noise."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.store = getattr(getattr(app, "state", None), "store", None)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        except (asyncio.CancelledError, GeneratorExit):
            pass


async def _optional_json(request: Request) -> Optional[Dict[str, Any]]:
    """A request's JSON object body, ``{}`` when it has none, ``None`` when it is not one."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _when(raw: Optional[str]) -> Optional[float]:
    """``2h`` ago, ``1d`` ago, or a Unix time; ``None`` for nothing."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except ValueError:
        return parse_since(raw)
    # A bare number this large is a Unix time, not a number of minutes.
    return value if value > 1e9 else parse_since(raw)


def create_app(
    nornir: Nornir,
    *,
    sample_interval: Optional[int] = None,
    resync_interval: int = 300,
    refresh: float = DEFAULT_REFRESH,
    workers: int = 20,
    idle_timeout: float = 900.0,
    restart_debounce: float = 1.0,
    connect_retry_interval: float = 30.0,
    topo_name: Optional[str] = None,
    fabric_source: Optional[str] = None,
    snapshot_dir: Optional[Path] = None,
    watch_interval: float = 0.0,
    persist_acks: bool = False,
    watch_prefixes: Sequence[str] = (),
    chat_client_factory: Optional[Callable[[], Any]] = None,
    jsonrpc_call: Optional[Callable[..., Any]] = None,
    history: bool = True,
    history_dir: Optional[Path] = None,
    retention_days: float = DEFAULT_RETENTION_DAYS,
) -> Starlette:
    """Build the fcli server application around an initialized Nornir inventory.

    With *history* and a *watch_interval*, the timeline, the baselines and
    the configurations the nodes commit are kept on disk, one SQLite file per
    fabric in *history_dir* (beside the snapshots, by default).
    """
    snapshot_store = SnapshotStore(snapshot_dir)
    # The cables are kept beside the snapshots, one file per fabric, so a
    # server restarted during an outage still knows what the down link was.
    cabling_file = (
        snapshot_store.directory.parent / "cabling" / f"{_slug(topo_name or 'fabric')}.json"
        if watch_interval > 0
        else None
    )
    history_store: Optional[HistoryStore] = None
    if history and watch_interval > 0:
        try:
            history_store = HistoryStore.for_fabric(
                topo_name or "fabric", history_dir or snapshot_store.directory.parent / "history"
            )
        except (HistoryError, OSError) as exc:
            logger.warning("running without a history: %s", exc)
    store = FabricStore(
        nornir,
        history=history_store,
        retention_days=retention_days,
        sample_interval=sample_interval,
        resync_interval=resync_interval,
        workers=workers,
        idle_timeout=idle_timeout,
        restart_debounce=restart_debounce,
        connect_retry_interval=connect_retry_interval,
        topo_name=topo_name,
        watch_interval=watch_interval,
        cabling_file=cabling_file,
        # In memory unless asked otherwise: then kept beside the cabling, one
        # file per fabric, so a restart does not bring back everything that
        # was acknowledged.
        ack_file=(
            snapshot_store.directory.parent / "acks" / f"{_slug(topo_name or 'fabric')}.json"
            if persist_acks
            else None
        ),
    )
    for prefix in watch_prefixes:
        store.timeline.watch(prefix)
    chat_kwargs: Dict[str, Any] = {}
    if chat_client_factory is not None:
        chat_kwargs["client_factory"] = chat_client_factory
    if jsonrpc_call is not None:
        chat_kwargs["jsonrpc_call"] = jsonrpc_call
    chat = ChatService(store, **chat_kwargs)
    chat_gate = asyncio.Semaphore(1)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await anyio.to_thread.run_sync(store.start)
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(store.stop)

    async def index(_request: Request) -> Response:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            html.replace(ASSET_TOKEN, asset_version()),
            headers={"Cache-Control": "no-cache"},
        )

    async def reports(_request: Request) -> Response:
        return JSONResponse(
            {
                "version": __version__,
                "topo_name": store.topo_name,
                # Where the fabric's name came from: the lab, the Nornir config
                # or the command line, so the badge says what it names.
                "fabric_source": fabric_source or ("clab" if store.topo_name else None),
                # The lenses are offered alongside the reports, marked as what
                # they are: a question with arguments, run rather than streamed.
                "reports": [r.as_dict() for r in reports_for(SERVER)]
                + [lens.as_dict() for lens in lenses_for(SERVER)],
                "chat": {
                    "enabled": chat.enabled(),
                    "providers": chat.providers(),
                },
            }
        )

    async def inventory(_request: Request) -> Response:
        hosts = await anyio.to_thread.run_sync(store.inventory)
        return JSONResponse({"hosts": hosts})

    async def status(_request: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(store.status))

    async def network_instances(request: Request) -> Response:
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        found = await anyio.to_thread.run_sync(store.network_instances, inv_filter)
        return JSONResponse({"network_instances": found})

    async def overview(request: Request) -> Response:
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        return JSONResponse(await anyio.to_thread.run_sync(store.overview, inv_filter))

    async def topology(request: Request) -> Response:
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        return JSONResponse(await anyio.to_thread.run_sync(store.topology, inv_filter))

    async def timeline(_request: Request) -> Response:
        """Where the timeline stands: how much it holds, and when the baseline is from."""
        return JSONResponse({**store.timeline.status(), "watch_interval": store.watch_interval})

    async def acks(_request: Request) -> Response:
        """Every acknowledged finding."""
        return JSONResponse({"acks": [dict(a.__dict__) for a in store.acks.all()]})

    async def ack_change(request: Request) -> Response:
        """Acknowledge an incident, or take the acknowledgement off it.

        Takes ``{"incident": <id>, "note": "...", "inv_filter": "k=v"}``; the
        path says which.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - bad client body
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        incident = body.get("incident") if isinstance(body, dict) else None
        if not isinstance(incident, str) or not incident:
            return JSONResponse({"error": "give the 'incident' id"}, status_code=400)
        note = body.get("note") or ""
        if not isinstance(note, str):
            return JSONResponse({"error": "note must be a string"}, status_code=400)
        # The filter the page was rendered with, which the incident's id is of.
        inv_filter = parse_kv(body.get("inv_filter") or None)
        acknowledging = request.url.path.endswith("/ack")
        try:
            if acknowledging:
                result = await anyio.to_thread.run_sync(store.acknowledge, incident, note, inv_filter)
            else:
                result = await anyio.to_thread.run_sync(store.unacknowledge, incident, inv_filter)
        except KeyError as exc:
            return JSONResponse({"error": exc.args[0] if exc.args else "no such incident"}, status_code=404)
        return JSONResponse(result)

    async def ack_all(request: Request) -> Response:
        """Acknowledge every open incident: ``{"note": "...", "inv_filter": "k=v"}``."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - bad client body
            body = {}
        body = body if isinstance(body, dict) else {}
        note = body.get("note") or ""
        if not isinstance(note, str):
            return JSONResponse({"error": "note must be a string"}, status_code=400)
        inv_filter = parse_kv(body.get("inv_filter") or None)
        result = await anyio.to_thread.run_sync(store.acknowledge_all, note, inv_filter)
        return JSONResponse(result)

    async def watched(_request: Request) -> Response:
        """The prefixes whose changes the timeline reports one by one."""
        return JSONResponse({"watched": store.timeline.watched()})

    async def watch_change(request: Request) -> Response:
        """Watch a prefix, or stop: ``{"prefix": "10.1.4.16/32"}``; the path says which."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - bad client body
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        prefix = body.get("prefix") if isinstance(body, dict) else None
        if not isinstance(prefix, str) or not prefix.strip():
            return JSONResponse({"error": "give the 'prefix' to watch"}, status_code=400)
        try:
            if request.url.path.endswith("/unwatch"):
                if not store.timeline.unwatch(prefix):
                    return JSONResponse({"error": f"'{prefix}' is not watched"}, status_code=404)
            else:
                store.timeline.watch(prefix)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"watched": store.timeline.watched()})

    async def baseline(request: Request) -> Response:
        """Keep the fabric as it is now as the baseline it is compared against.

        Takes an optional ``{"name": "before-upgrade", "note": "..."}``.
        """
        body = await _optional_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        name, note = body.get("name"), body.get("note") or ""
        if name is not None and not isinstance(name, str) or not isinstance(note, str):
            return JSONResponse({"error": "name and note must be strings"}, status_code=400)
        try:
            status = await anyio.to_thread.run_sync(store.set_baseline, name, note)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(status)

    async def baselines(_request: Request) -> Response:
        """The baselines kept, and which one the fabric is compared against."""
        return JSONResponse(await anyio.to_thread.run_sync(store.baselines))

    async def baseline_use(request: Request) -> Response:
        """Compare against a kept baseline: ``{"name": "..."}``, or ``null`` for the latest reading."""
        body = await _optional_json(request)
        if body is None or "name" not in body:
            return JSONResponse({"error": "give the 'name' of a baseline, or null"}, status_code=400)
        try:
            return JSONResponse(await anyio.to_thread.run_sync(store.use_baseline, body["name"]))
        except KeyError as exc:
            return JSONResponse({"error": exc.args[0]}, status_code=404)

    async def baseline_delete(request: Request) -> Response:
        try:
            return JSONResponse(await anyio.to_thread.run_sync(store.delete_baseline, request.path_params["name"]))
        except KeyError as exc:
            return JSONResponse({"error": exc.args[0]}, status_code=404)

    async def history_changes(request: Request) -> Response:
        """Changes from the history on disk, beyond what the timeline holds in memory.

        Query: ``since`` and ``until`` (``2h``, ``1d``, or a Unix time),
        ``node`` and ``kind`` (comma-separated), ``limit`` (default 1000).
        """
        if store.history is None:
            return JSONResponse({"error": "the server runs without a history"}, status_code=404)
        query = request.query_params
        try:
            since = _when(query.get("since"))
            until = _when(query.get("until"))
            limit = int(query.get("limit") or 1000)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        nodes = [n for n in (query.get("node") or "").split(",") if n] or None
        kinds = [k for k in (query.get("kind") or "").split(",") if k] or None
        found = await anyio.to_thread.run_sync(
            functools.partial(store.history.changes, since=since, until=until, nodes=nodes, kinds=kinds, limit=limit)
        )
        return JSONResponse({"changes": [{"at": c.at, **as_row(c)} for c in found]})

    async def config_versions(request: Request) -> Response:
        """The configurations kept after each commit: ``?node=leaf1`` for one node."""
        node = request.query_params.get("node") or None
        return JSONResponse({"configs": await anyio.to_thread.run_sync(store.config_versions, node)})

    def _commit(raw: Optional[str]) -> Optional[int]:
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"'{raw}' is not a commit id") from None

    async def config_text(request: Request) -> Response:
        """One node's configuration after a commit (``?commit=``, the newest by default), as set lines."""
        try:
            commit = _commit(request.query_params.get("commit"))
            result = await anyio.to_thread.run_sync(store.config_text, request.path_params["node"], commit)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": exc.args[0]}, status_code=404)
        return JSONResponse(result)

    async def config_diff(request: Request) -> Response:
        """What a commit changed: ``?commit=`` (the newest by default), ``?against=`` another commit."""
        try:
            commit = _commit(request.query_params.get("commit"))
            against = _commit(request.query_params.get("against"))
            result = await anyio.to_thread.run_sync(store.config_diff, request.path_params["node"], commit, against)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": exc.args[0]}, status_code=404)
        return JSONResponse(result)

    def streamable_report(name: str) -> ReportSpec:
        """The named report, provided the server is able to stream it."""
        report = get_report(name)
        if not report.on(SERVER):
            raise KeyError(f"report '{name}' cannot be streamed")
        return report

    def renderer(
        request: Request, *, progressive: bool = False, load: Optional[ReportLoad] = None
    ) -> Callable[[], Dict[str, Any]]:
        """What answers the named report or lens, with the request's arguments.

        A lens and a report are asked for the same way and answer in the same
        shape; only what the server does to answer differs. Raises
        :class:`KeyError` for a name the server does not offer and
        :class:`ValueError` for an argument it cannot use.
        """
        name = request.path_params["name"]
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        if name in LENSES_BY_NAME and LENSES_BY_NAME[name].on(SERVER):
            lens = LENSES_BY_NAME[name]
            params = coerce_lens_params(lens, request.query_params)
            return lambda: store.lens_table(lens, inv_filter, params)
        report = streamable_report(name)
        params = coerce_params(report, request.query_params)
        hosts = parse_nodes(request.query_params.get("node"))
        gradual = progressive and full_table_read(report, params)
        return lambda: store.table(report, inv_filter, params, hosts, progressive=gradual, load=load if gradual else None)

    def progress_of(request: Request) -> Callable[[], Dict[str, Any]]:
        """How far the nodes are with what the named report or lens reads."""
        name = request.path_params["name"]
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        lens = LENSES_BY_NAME.get(name)
        if lens is not None and lens.on(SERVER):
            return lambda: store.progress(list(lens.requires), inv_filter)
        hosts = parse_nodes(request.query_params.get("node"))
        try:
            report = streamable_report(name)
            activated = store.activation_name(report, coerce_params(report, request.query_params))
        except (KeyError, ValueError):
            activated = name
        return lambda: store.progress([activated], inv_filter, hosts)

    async def report_keys(request: Request) -> Response:
        """The values a key parameter of a report takes, for a surface to offer."""
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        param = request.query_params.get("param", "")
        if not any(spec.name == param and spec.kind == "rib-key" for spec in report.params):
            return JSONResponse({"error": f"'{param}' is not a key of {report.name}"}, status_code=400)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        try:
            chosen = coerce_params(report, request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        keys = {k: v for k, v in chosen.items() if any(s.name == k and s.kind == "rib-key" for s in report.params)}
        try:
            found = await anyio.to_thread.run_sync(
                lambda: store.key_values(report, param, inv_filter, keys, request.query_params.get("q", ""))
            )
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse(found)

    async def report_once(request: Request) -> Response:
        try:
            render = renderer(request)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            body = await anyio.to_thread.run_sync(lambda: serialize_table(render()).body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return Response(body, media_type="application/json")

    async def report_stream(request: Request) -> Response:
        load = ReportLoad()
        try:
            render = renderer(request, progressive=True, load=load)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            interval = max(float(request.query_params.get("refresh", refresh)), 0.5)
        except ValueError:
            interval = refresh

        async def events() -> AsyncIterator[bytes]:
            try:
                async for event in table_events(
                    store, request.path_params["name"], render, interval,
                    request.is_disconnected, progress_of(request),
                ):
                    yield event
            finally:
                store.cancel_load(load)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # ------------------------------------------------------------------ #
    # snapshots and comparison
    # ------------------------------------------------------------------ #

    async def snapshots(request: Request) -> Response:
        report = request.query_params.get("report") or None
        saved = await anyio.to_thread.run_sync(snapshot_store.list, report)
        return JSONResponse({"snapshots": [s.as_dict() for s in saved]})

    async def snapshot_save(request: Request) -> Response:
        """Render *name* now, and keep it to compare against later."""
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        try:
            params = coerce_params(report, request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        label = request.query_params.get("label", "")
        table = await anyio.to_thread.run_sync(store.table, report, inv_filter, params)
        saved = await anyio.to_thread.run_sync(
            functools.partial(
                snapshot_store.save,
                report.name,
                table,
                label=label,
                inv_filter=inv_filter,
                params=params,
                fabric=store.topo_name or "",
                inventory=store.targets(inv_filter),
            )
        )
        return JSONResponse(saved.as_dict(), status_code=201)

    async def snapshot_delete(request: Request) -> Response:
        removed = await anyio.to_thread.run_sync(
            snapshot_store.delete, request.path_params["snapshot_id"]
        )
        if not removed:
            return JSONResponse({"error": "no such snapshot"}, status_code=404)
        return JSONResponse({"deleted": request.path_params["snapshot_id"]})

    async def report_diff(request: Request) -> Response:
        """Compare a report against a snapshot of it, or one node against another."""
        try:
            report = streamable_report(request.path_params["name"])
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        inv_filter = parse_kv(request.query_params.get("inv_filter"))
        try:
            params = coerce_params(report, request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        include_same = request.query_params.get("same") in ("1", "true", "yes")

        against = request.query_params.get("against")
        nodes = request.query_params.get("nodes")
        if bool(against) == bool(nodes):
            return JSONResponse(
                {"error": "give either 'against=<snapshot id>' or 'nodes=<a>,<b>'"},
                status_code=400,
            )

        table = await anyio.to_thread.run_sync(store.table, report, inv_filter, params)

        if nodes:
            wanted = [n.strip() for n in nodes.split(",") if n.strip()]
            if len(wanted) != 2:
                return JSONResponse(
                    {"error": "'nodes' takes exactly two node names"}, status_code=400
                )
            return JSONResponse(
                diff_nodes(
                    table,
                    wanted[0],
                    wanted[1],
                    report.key_columns,
                    include_same=include_same,
                )
            )

        snapshot = await anyio.to_thread.run_sync(snapshot_store.get, str(against))
        if snapshot is None:
            return JSONResponse({"error": "no such snapshot"}, status_code=404)
        if snapshot.report != report.name:
            return JSONResponse(
                {"error": f"that snapshot is of report '{snapshot.report}'"},
                status_code=400,
            )
        mismatch = comparable(
            snapshot,
            inv_filter,
            params,
            fabric=store.topo_name or "",
            inventory=store.targets(inv_filter),
        )
        if mismatch:
            return JSONResponse({"error": mismatch}, status_code=409)
        return JSONResponse(
            diff_tables(
                snapshot.table,
                table,
                report.key_columns,
                labels=(snapshot.label, "now"),
                include_same=include_same,
            )
        )

    async def chat_turn(request: Request) -> Response:
        if not chat.enabled():
            return JSONResponse({"error": NO_PROVIDER}, status_code=503)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - bad client body
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON object expected"}, status_code=400)
        messages = body.get("messages")
        if not isinstance(messages, list):
            return JSONResponse({"error": "messages must be a list"}, status_code=400)
        context = body.get("context")
        if context is not None and not isinstance(context, dict):
            return JSONResponse({"error": "context must be an object"}, status_code=400)
        provider = body.get("provider")
        if provider is not None and not isinstance(provider, str):
            return JSONResponse({"error": "provider must be a string"}, status_code=400)
        effort = body.get("effort")
        if effort is not None and not isinstance(effort, str):
            return JSONResponse({"error": "effort must be a string"}, status_code=400)
        try:
            await asyncio.wait_for(chat_gate.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": "another chat is in progress"}, status_code=429
            )

        async def gated() -> AsyncIterator[bytes]:
            try:
                async for chunk in chat.events(messages, context, provider, effort):
                    yield chunk
            finally:
                chat_gate.release()

        return StreamingResponse(
            gated(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    routes = [
        Route("/", index),
        Route("/api/reports", reports),
        Route("/api/inventory", inventory),
        Route("/api/status", status),
        Route("/api/network-instances", network_instances),
        Route("/api/overview", overview),
        Route("/api/topology", topology),
        Route("/api/timeline", timeline),
        Route("/api/baseline", baseline, methods=["POST"]),
        Route("/api/baselines", baselines),
        Route("/api/baseline/use", baseline_use, methods=["POST"]),
        Route("/api/baseline/{name}", baseline_delete, methods=["DELETE"]),
        Route("/api/history", history_changes),
        Route("/api/configs", config_versions),
        Route("/api/config/{node}", config_text),
        Route("/api/config/{node}/diff", config_diff),
        Route("/api/acks", acks),
        Route("/api/watch", watched),
        Route("/api/watch", watch_change, methods=["POST"]),
        Route("/api/unwatch", watch_change, methods=["POST"]),
        Route("/api/ack", ack_change, methods=["POST"]),
        Route("/api/unack", ack_change, methods=["POST"]),
        Route("/api/ack-all", ack_all, methods=["POST"]),
        Route("/api/keys/{name}", report_keys),
        Route("/api/report/{name}", report_once),
        Route("/api/stream/{name}", report_stream),
        Route("/api/diff/{name}", report_diff),
        Route("/api/snapshots", snapshots),
        Route("/api/snapshot/{name}", snapshot_save, methods=["POST"]),
        Route("/api/snapshot/{snapshot_id}", snapshot_delete, methods=["DELETE"]),
        Route("/api/chat", chat_turn, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.store = store
    app.state.chat = chat
    wrapped = SuppressCancelledErrorMiddleware(app)
    wrapped.store = store
    return wrapped


class _QuietUvicornShutdown(logging.Filter):
    """Drop uvicorn's timeout-on-Ctrl+C ERROR; we tear gNMI down ourselves."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "timeout graceful shutdown exceeded" not in record.getMessage()


def serve(
    nornir: Nornir,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    sample_interval: Optional[int] = None,
    resync_interval: int = 300,
    refresh: float = DEFAULT_REFRESH,
    workers: int = 20,
    idle_timeout: float = 900.0,
    log_level: str = "info",
    topo_name: Optional[str] = None,
    fabric_source: Optional[str] = None,
    snapshot_dir: Optional[Path] = None,
    watch_interval: float = 15.0,
    persist_acks: bool = False,
    watch_prefixes: Sequence[str] = (),
    history: bool = True,
    history_dir: Optional[Path] = None,
    retention_days: float = DEFAULT_RETENTION_DAYS,
) -> None:
    """Run the fcli server with uvicorn (blocking)."""
    import uvicorn

    app = create_app(
        nornir,
        sample_interval=sample_interval,
        resync_interval=resync_interval,
        refresh=refresh,
        workers=workers,
        idle_timeout=idle_timeout,
        topo_name=topo_name,
        fabric_source=fabric_source,
        snapshot_dir=snapshot_dir,
        watch_interval=watch_interval,
        persist_acks=persist_acks,
        watch_prefixes=watch_prefixes,
        history=history,
        history_dir=history_dir,
        retention_days=retention_days,
    )
    store = app.store
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=False,
        timeout_graceful_shutdown=5.0,
    )
    server = uvicorn.Server(config)
    # Tear the store down as soon as SIGINT/SIGTERM arrives, not after uvicorn
    # has already waited out timeout_graceful_shutdown. SSE streams sit in
    # in-flight Gets; closing gNMI first lets those tasks finish so uvicorn
    # never logs "Cancel N running task(s), timeout graceful shutdown exceeded".
    original_exit = server.handle_exit

    def handle_exit(sig: int, frame: Any) -> None:
        threading.Thread(target=store.stop, name="fcli-shutdown", daemon=True).start()
        original_exit(sig, frame)

    server.handle_exit = handle_exit  # type: ignore[method-assign]
    logging.getLogger("uvicorn.error").addFilter(_QuietUvicornShutdown())
    try:
        server.run()
    except (KeyboardInterrupt, SystemExit):
        pass
