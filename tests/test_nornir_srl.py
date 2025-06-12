import sys
import types

dummy_pg = types.ModuleType("pygnmi")
dummy_client = types.ModuleType("pygnmi.client")
class Dummy(object):
    pass
dummy_client.gNMIclient = Dummy
dummy_pg.client = dummy_client
sys.modules.setdefault("pygnmi", dummy_pg)
sys.modules.setdefault("pygnmi.client", dummy_client)

dummy_nornir_cfg = types.ModuleType("nornir.core.configuration")
class DummyConfig:
    pass
dummy_nornir_cfg.Config = DummyConfig
sys.modules.setdefault("nornir.core.configuration", dummy_nornir_cfg)

dummy_nornir_exc = types.ModuleType("nornir.core.exceptions")
class DummyExc(Exception):
    pass
dummy_nornir_exc.ConnectionException = DummyExc
sys.modules.setdefault("nornir.core.exceptions", dummy_nornir_exc)

dummy_jmes = types.ModuleType("jmespath")
def _noop(*args, **kwargs):
    return {}
dummy_jmes.search = _noop
sys.modules.setdefault("jmespath", dummy_jmes)

from nornir_srl import __version__
from nornir_srl.connections.srlinux import SrLinux


def test_version():
    assert __version__ == '0.2.22'


class DummyConn:
    def __init__(self):
        self.called = False
        self.kw = {}

    def subscribe(self, **kwargs):
        self.called = True
        self.kw = kwargs
        return "ok"


def test_subscribe_wrapper():
    dev = SrLinux()
    dev._connection = DummyConn()
    result = dev.subscribe(paths=["/system/name"], mode="once", timeout=1)
    assert result == "ok"
    assert dev._connection.called is True
    assert dev._connection.kw["path"] == ["/system/name"]
    assert dev._connection.kw["mode"] == "once"
    assert dev._connection.kw["timeout"] == 1

