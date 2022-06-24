import pprint
import json
from typing import Any, Dict, Type, Optional

from pygnmi.client import gNMIclient
from nornir.core.plugins.connections import ConnectionPlugin

class gNMIClient(ConnectionPlugin):
    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    @property
    def connection(self) -> Any:
        pass



target = ("clab-4l2s-l1", 57400)
cert = "clab-4l2s/ca/root/root-ca.pem"

gc = gNMIclient(target=target, username="admin", password="admin",
        path_cert=cert)

gc.connect()

paths = [
        "interface[name=ethernet-1/48]",
        "interface[name=ethernet-1/49]",
        ]
# paths = ["interface"]

rx_upds = []
data = gc.get(path=paths, datatype="config", encoding="json_ietf")
print("get output")
# pprint.pprint(data)
print(json.dumps(data, indent=2,sort_keys=True))

for notif in data.get("notification"):
    if not "update" in notif:
        print(f"Notif has no updates for {paths}")
        exit(1)
    else:
        rx_upds = [ u for u in notif.get("update") ]

print("rx_upds")
# pprint.pprint(rx_upds, indent=2,sort_keys=True)
print(json.dumps(rx_upds, indent=2,sort_keys=True))

tx_upds = []
for u in rx_upds:
   tx_upds.append((u.get("path"), u.get("val")))
print("tx_upds")
# pprint.pprint(tx_upds)
print(json.dumps(tx_upds, indent=2,sort_keys=True))

r = gc.set(update=tx_upds, encoding="json_ietf")
print("Return")

pprint.pprint(r)


