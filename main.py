from typing import Any, Dict, List, Optional
from collections import OrderedDict

from nornir import InitNornir
from nornir.core.task import Result, Task, AggregatedResult

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.box import    MINIMAL_DOUBLE_HEAD


from nornir_utils.plugins.functions import print_result
#from nornir_srl.gnmi import get_path
#from nornir_srl.load import Intent
# from nornir_srl.tasks import gnmi_get, gnmi_set

from nornir_srl.connections.srlinux import CONNECTION_NAME

def get_version(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result=device.get_info())

def get_itfs(task: Task) -> Result:
    device = task.host.get_connection(CONNECTION_NAME, task.nornir.config)
    return Result(host=task.host, result=device.get_itfs())



def rich_table(results: AggregatedResult) -> None:
    console = Console()

    for hostname, host_result in results.items():
        table = Table(box=MINIMAL_DOUBLE_HEAD)
        table.add_column(hostname, justify="right", style="cyan", no_wrap=True)
        table.add_column("result")
        table.add_column("changed")

        for r in host_result:
            text = Text()
            if r.failed:
                text.append(f"{r.exception}", style="red")
            else:
                text.append(f"{r.result or ''}", style="green")

            changed = Text()
            if r.changed:
                color = "orange3"
            else:
                color = "green"
            changed.append(f"{r.changed}", style=color)

            table.add_row(r.name, text, changed)

        console.print(table)

def rich_table2(results: AggregatedResult) -> None:  # for results with object per host
    
    console = Console()
    d = {}
    fields = set()
    for hostname, host_result in results.items():
        for r in host_result:
            if r.name in d:
                d[r.name].update({ hostname: r.result })
            else:
                d[r.name] = { hostname: r.result }
            for k in r.result.keys():
                fields.add(k)

    for name, result_per_host in d.items():
        table = Table(title=name, box=MINIMAL_DOUBLE_HEAD)
        table.add_column('device', no_wrap=True)
        for f in fields:
            table.add_column(f)
        for h, r in result_per_host.items():
            values = [ r.get(f) for f in fields ]
            table.add_row(h, *values)

    console.print(table)

#def main():
#intents = Intent.from_files("intent")
nr0 = InitNornir(config_file="nornir_config.yaml")
# result = nr0.run(task=gnmi_get, type="config", strip_module=True, paths=["/interface[name=ethernet-1/48]", "/interface[name=ethernet-1/49]"])
p = [
        ("interface[name=ethernet-1/48]", 
        {
            "description": "itf description e-1/48",
            "admin-state": "enable",
            "mtu": 1501,
        })
]
#result = nr0.run(task=gnmi_get, type="config", paths=["/system/gnmi-server"], strip_module=True)
# result = nr0.run(task=gnmi_set, action="update", dry_run=False, paths=p, encoding="json_ietf")
r = nr0.run(task=get_version)

# print_result(r)

rich_table(r)

from nornir_srl.tasks.srl_config import configure_device
# nr1 = nr0.filter(hostname='clab-4l2s-l1')

# nr0 = nr0.filter(hostname='clab-4l2s-l2')
r = nr0.run(task=configure_device, intent_path="./intent/vars", dry_run=False)

print_result(r)


# if __name__ == "__main__":
#     main()


